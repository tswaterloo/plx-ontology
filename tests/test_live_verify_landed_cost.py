"""case.packaging_landed_cost through ``run_spec``.

The spec's own command (``scripts.eval_packaging_oracle_check landed-cost``) runs in-process
over the packaging fixture's FakeWorkbench (PO#5447, its lines at $0.000) and an injected
Graph transport serving the AGENTS site's '/Ontology UseCases' folder with one SYNTHETIC zip:
two proforma invoices that disagree (37,600 and 40,100 USD), a destination-only 40HQ quote
and an air all-in quote, each naming the purchase order. The registry is synthetic too: it
pins the zip and its members under the spec's own pin ids (and the second proforma under an
id of its own). The report is replayed through the committed spec with ``run_spec``: a pass;
a fail when a cited member's bytes differ from its pin; blocked with the typed
``quote_not_linked_to_po`` when a quote names no purchase order (never a pass).

Evidence label: injected-transport. No network I/O, no credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.live_verify_helpers import FAKE_MODULE, SECRET, FakeRoot
from evals.oracle import landed_cost as oracle
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from ontology.tests.test_container_members import zipped
from operating.host import HostError
from operating.mcp_server import _marking_grants, add_host_arguments
from scripts import eval_packaging_oracle_check as harness

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "devloop" / "live-checks" / "case.packaging_landed_cost.json")
                  .read_text("utf-8"))
FIXTURE = json.loads((ROOT / "operating" / "tests" / "fixtures" / "packaging_case_fixture.json")
                     .read_text("utf-8"))
ARGS = SPEC["command"][3:]
[PROFORMA] = [ARGS[i + 1] for i, arg in enumerate(ARGS) if arg == "--proforma"]
DEST, AIR = [ARGS[i + 1] for i, arg in enumerate(ARGS) if arg == "--quote"]
ZIP_PIN, SECOND_PI = "co-synthzip0001", "co-synthpi00002"
# SYNTHETIC documents shaped like the packaging case's (no real body, price or name).
PIS = {"pi-a.pdf": ["PROFORMA INVOICE", "PI No: PI-SYN-A", "Date: 2026-09-10",
                    "Customer PO: PO5447", "Quantity: 40,000 pcs", "Unit Price: USD 0.94/pc",
                    "Total Amount: USD 37,600.00"],
       "pi-b.pdf": ["PROFORMA INVOICE", "PI No: PI-SYN-B", "Date: 2026-09-20",
                    "Customer PO: PO5447", "Quantity: 40,000 pcs",
                    "Unit Price: USD 1.0025/pc", "Total Amount: USD 40,100.00"]}
QUOTES = {"quote-40hq.pdf": ["FREIGHT QUOTATION", "Quote No: FQ-SYN-1", "Ref PO: PO5447",
                             "Scope: destination charges", "Basis: per 40HQ", "Containers: 1",
                             "Includes: destination charges, delivery",
                             "Excludes: duties and taxes", "Valid until: 2099-01-31",
                             "Total amount: USD 1,710.00"],
          "quote-air.pdf": ["FREIGHT QUOTATION", "Quote No: FQ-SYN-2", "Ref PO: PO5447",
                            "Scope: door to door, air freight, all-in", "Basis: per shipment",
                            "Includes: pickup, export clearance, air freight, delivery",
                            "Excludes: duties and taxes, listed surcharges and contingencies",
                            "Valid until: 2099-01-31", "All-in total: USD 83,965.56"]}
# The example host config's grant of the finance marking: its verifier principal only.
GRANTS = json.loads((ROOT / "examples" / "packaging_landed_cost_host.live-check.json")
                    .read_text("utf-8"))["marking_grants"]
[VERIFIER] = GRANTS["finance"]
PINS = {"pi-a.pdf": PROFORMA, "pi-b.pdf": SECOND_PI, "quote-40hq.pdf": DEST,
        "quote-air.pdf": AIR}


def _pdf(lines: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, invariant=1)   # the same bytes on every build
    for y, line in enumerate(lines):
        page.drawString(60, 780 - 18 * y, line)
    page.showPage()
    page.save()
    return buffer.getvalue()


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("network I/O attempted")


def _row(pin: str, kind: str, content: bytes, parent: str | None) -> dict:
    return {"id": pin, "status": "pinned", "kind": kind, "parent": parent, "source": {},
            "expected": {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}}


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "the proforma invoices and quotes are read from PDFs: needs pypdf and "
                     "reportlab")
class LandedCostLiveCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.verifier = FakeRoot()
        self.addCleanup(self.verifier.close)

    def run_check(self, pinned: dict[str, bytes], served: dict[str, bytes], *,
                  grants: object = GRANTS, drop: tuple[str, ...] = ()
                  ) -> tuple[int, dict, dict]:
        """The spec's command over ``served`` (member name -> bytes, zipped into the
        use-case folder) with a registry pinning ``pinned``; the report and its
        ``run_spec`` replay. ``grants``: the host config's ``marking_grants`` (None: the
        key left out); ``drop``: ``--principal`` values left out of the command."""
        original = zipped(sorted(pinned.items()))
        registry = {"schema": "plx-case-originals/v1",
                    "rows": [_row(ZIP_PIN, "original", original, None)]
                    + [_row(PINS[name], "archive_member", data, ZIP_PIN)
                       for name, data in sorted(pinned.items())]}
        (self.temp / "registry.json").write_text(json.dumps(registry), "utf-8")
        drive = tsp.FakeDrive()
        drive.folder("01USECASES", "Ontology UseCases", drive=tsp.SITE_DRIVE)
        drive.file("01PACKET", "packaging-case.zip", "01USECASES", zipped(sorted(served.items())),
                   mime="application/zip", drive=tsp.SITE_DRIVE)
        fake = FakeWorkbench()
        fake.catalog = copy.deepcopy(FIXTURE["catalog"])
        fake.tables = copy.deepcopy(FIXTURE["tables"])
        for line in fake.tables["public.app_purchase_order_line"]:
            line["unit_price"] = "0.000"   # the purchase order was sent at $0.000
        examples = ROOT / "examples"
        config = json.loads((examples / "packaging_landed_cost_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(examples / config[key])
        config["sharepoint"]["scope"] = str(examples / config["sharepoint"]["scope"])
        config.pop("marking_grants")
        if grants is not None:
            config["marking_grants"] = grants
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        args = [arg for i, arg in enumerate(ARGS) if arg not in drop
                and not (arg == "--principal" and ARGS[i + 1] in drop)]
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(self.temp / "registry.json") if arg == "examples/case_originals.json"
                else arg.replace("{tmp}", str(self.temp)) for arg in args]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = harness.main(argv, environ={
                **tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY},
                host_options={"sender": drive, "workbench_fetch": fake,
                              "sleep": lambda _s: None})
        document = json.loads(out.getvalue())
        self.assertNotIn(KEY, out.getvalue())
        self.assertFalse([u for m, u in drive.calls if m != "GET"])
        check = self.verifier.run({**SPEC, "command": ["python3", "-m", FAKE_MODULE, "echo"]},
                                  environ={**self.verifier.env, **tsp.ENV,
                                           "PLX_WORKBENCH_API_URL": "https://portal.invalid",
                                           "PLX_WORKBENCH_API_KEY": SECRET,
                                           "FAKE_DOC": json.dumps(document),
                                           "FAKE_EXIT": str(code)})
        return code, document, check

    def test_the_cited_originals_agree_and_the_check_passes(self) -> None:
        members = {name: _pdf(lines) for name, lines in {**PIS, **QUOTES}.items()}
        code, document, check = self.run_check(members, members)
        verdict = document["oracle_check"]
        self.assertEqual((code, verdict["status"], verdict["reasons"]), (0, "agree", []),
                         document)
        self.assertEqual(check["result"], "pass", check)
        self.assertEqual((verdict["finance_granted"], verdict["unread_amounts"]), (True, 0))
        # goods: unknown, the two proforma amounts in one PI_VERSION_UNRESOLVED conflict
        self.assertEqual((verdict["goods"], verdict["version_conflict"],
                          verdict["confirmed_versions"]), ("unresolved", True, 2))
        self.assertTrue(verdict["duty_source_gap"] and verdict["total_unknown"])
        self.assertEqual(verdict["cited_documents"], verdict["cited_pinned"])
        quotes = {q["pin"]: q for q in verdict["quotes"]}
        self.assertIn("freight_scope_partial", quotes[DEST]["scope_codes"])
        self.assertTrue(quotes[AIR]["assessed"] and quotes[AIR]["scope_codes"])

    def test_finance_values_reach_only_the_principal_the_host_grants_finance(self) -> None:
        """Money is finance-marked: with no ``marking_grants`` nobody holds finance, so the
        command's readers (staff:operations and the verifier) see no confirmed amount; with
        the grant, staff:operations alone still sees none (only the granted verifier does,
        the passing check above); a grant to a wildcard fails closed before any read."""
        members = {name: _pdf(lines) for name, lines in {**PIS, **QUOTES}.items()}
        for grants, drop in ((None, ()), (GRANTS, (VERIFIER,))):
            code, document, check = self.run_check(members, members, grants=grants,
                                                   drop=drop)
            verdict = document["oracle_check"]
            # no confirmed amount reaches the answer: no goods value, no version conflict
            self.assertEqual((verdict["confirmation_amounts"], verdict["confirmed_versions"],
                              verdict["version_conflict"], verdict["finance_granted"]),
                             (0, 0, False, False), document)
            self.assertIn(verdict["goods"], (None, "unresolved"), document)
            # the cause is the check's own missing grant, typed: not an unlinked proforma
            self.assertIn(oracle.FINANCE_NOT_GRANTED, verdict["reasons"], document)
            self.assertNotIn(oracle.PI_NOT_LINKED, verdict["missing_codes"], document)
            self.assertEqual(verdict["status"], "disagree", document)
            self.assertNotEqual(check["result"], "pass", check)
            self.assertNotEqual(code, 0)
        for invalid in ({"finance": ["staff:*"]}, {"finance": "svc:x"}, {"unknown": ["svc:x"]},
                        {"finance": ["no-colon"]}, {"finance": ["staff:"]},
                        {"finance": [":ops"]}, {"finance": ["staff: ops"]}):
            with self.assertRaises(HostError) as refused:
                self.run_check(members, members, grants=invalid)
            self.assertEqual(str(refused.exception), "host_config_invalid:marking_grants")

    def test_a_cited_member_whose_bytes_differ_from_its_pin_fails(self) -> None:
        members = {name: _pdf(lines) for name, lines in {**PIS, **QUOTES}.items()}
        altered = dict(members, **{"quote-40hq.pdf": _pdf(
            [*QUOTES["quote-40hq.pdf"][:-2], "Valid until: 2099-02-28",
             QUOTES["quote-40hq.pdf"][-1]])})
        code, document, check = self.run_check(members, altered)
        self.assertEqual((code, document["oracle_check"]["status"]), (3, "disagree"), document)
        self.assertIn("cited_bytes_not_pinned", document["oracle_check"]["reasons"])
        self.assertEqual(check["result"], "fail", check)

    def test_a_quote_naming_no_purchase_order_is_blocked_with_its_typed_reason(self) -> None:
        lines = {**PIS, **QUOTES, "quote-air.pdf": [
            line for line in QUOTES["quote-air.pdf"] if not line.startswith("Ref PO")]}
        members = {name: _pdf(text) for name, text in lines.items()}
        code, document, check = self.run_check(members, members)
        self.assertEqual((code, document["case"]["missing"]), (0, "quote_not_linked_to_po"),
                         document)
        [missing] = document["oracle_check"]["missing"]
        self.assertEqual(missing["pin"], AIR)
        self.assertIn("purchase order", missing["needs"])
        self.assertEqual((check["result"], check["detail"]["cause"]), ("blocked", "source_gap"),
                         check)
        self.assertIn("quote_not_linked_to_po", check["detail"]["reason"])


    def test_goods_left_unknown_for_a_typed_engine_reason_end_blocked_not_failed(self) -> None:
        """A proforma in another currency than the purchase order's: the engine leaves the
        goods unknown with CONFIRMATION_CURRENCY_DIFFERS. The check ends blocked with that
        typed code and the value a person must supply, never a pass and never a failure."""
        euro = [line.replace("USD", "EUR") for line in PIS["pi-a.pdf"]]
        members = {name: _pdf(text) for name, text in {"pi-a.pdf": euro, **QUOTES}.items()}
        code, document, check = self.run_check(members, members)
        verdict = document["oracle_check"]
        self.assertEqual((code, verdict["status"], verdict["reasons"], verdict["goods"]),
                         (0, "missing", [], "unresolved"), document)
        self.assertEqual(document["case"]["missing"], "confirmation_currency_differs")
        [missing] = verdict["missing"]
        self.assertEqual(missing["code"], "confirmation_currency_differs")
        self.assertIn("currency", missing["needs"])
        self.assertEqual((check["result"], check["detail"]["cause"]), ("blocked", "source_gap"),
                         check)
        self.assertIn("confirmation_currency_differs", check["detail"]["reason"])

# SYNTHETIC proforma texts (oracle unit tests): one stating its total, a revision without.
PI_A = b"PROFORMA INVOICE\nQuantity: 1,000 pcs\nUnit Price: USD 2.50/pc\n" \
       b"Total Amount: USD 2,500.00\n"
PI_B = b"PROFORMA INVOICE\nQuantity: 1,000 pcs\nUnit Price: USD 2.75/pc\n"


def _figure(oid: str, prop: str, value: str | None, handle: str) -> dict:
    return {"name": f"{oid}_{prop}", "kind": "property", "value": value,
            "object_id": oid, "object_type": "SupplierConfirmation", "property": prop,
            "citation": {"l1_handle": handle, "field_path": prop}, "unit": None,
            "currency": "USD" if prop != "quantity" else None}


class LandedCostOracleTests(unittest.TestCase):
    """The oracle over SYNTHETIC answers: a goods value is graded against every
    confirmation's cited figures and against the bytes those figures cite."""

    def grade(self, answer: list[dict], *, served: dict[str, bytes] | None = None,
              gaps: list[dict] = (), conflicts: list[dict] = (),
              held: tuple[str, ...] | None = ("finance",), proformas=("pin-a", "pin-b"),
              text_of=lambda data: data.decode("utf-8"), pi_a: bytes = PI_A,
              pi_b: bytes = PI_B) -> dict:
        registry = {"rows": [{"id": pin, "status": "pinned",
                              "expected": {"sha256": hashlib.sha256(data).hexdigest()}}
                             for pin, data in (("pin-a", pi_a), ("pin-b", pi_b))]}
        served = {"h-a": pi_a, "h-b": pi_b} if served is None else served
        total = {"name": "landed_cost", "kind": "computed", "value": None}
        return oracle.check({"answer": [*answer, total], "gaps": list(gaps),
                             "conflicts": list(conflicts)}, served.get, registry,
                            quotes=[], proformas=list(proformas), text_of=text_of,
                            held_markings=held)

    def figures(self, b_amount: str | None = None, b_price: str = "2.75",
                b_quantity: str = "1000") -> list[dict]:
        return [_figure("sc-a", "amount", "2500.00", "h-a"),
                _figure("sc-a", "unit_price", "2.50", "h-a"),
                _figure("sc-a", "quantity", "1000", "h-a"),
                _figure("sc-b", "amount", b_amount, "h-b"),
                _figure("sc-b", "unit_price", b_price, "h-b"),
                _figure("sc-b", "quantity", b_quantity, "h-b")]

    def test_one_amount_beside_a_second_confirmation_needs_every_figure_to_agree(self):
        """Two confirmations, both amounts read and equal: the goods are that amount only
        while their unit prices and quantities (unit price x quantity) agree too."""
        goods = {"name": "line_1_goods", "kind": "computed", "value": "2500.00"}
        same = self.grade([*self.figures("2500.00", "2.50"), goods], pi_b=PI_A + b"\n")
        self.assertEqual((same["status"], same["goods"], same["confirmed_versions"]),
                         ("agree", "one_amount", 1), same)
        pi_b = PI_A.replace(b"1,000", b"1,200")
        other = self.grade([*self.figures("2500.00", "2.50", "1200"), goods], pi_b=pi_b)
        self.assertEqual(other["status"], "disagree", other)
        self.assertIn("goods_picked_a_version", other["reasons"])
        self.assertNotIn("cited_value_not_in_bytes", other["reasons"])

    def test_goods_picked_beside_a_confirmation_whose_amount_is_unread_never_agrees(self):
        goods = {"name": "line_1_goods", "kind": "computed", "value": "2500.00"}
        verdict = self.grade([*self.figures(), goods])
        self.assertEqual(verdict["status"], "disagree", verdict)
        self.assertEqual(verdict["unread_amounts"], 1)
        # the second version's unit price differs too: a picked version either way
        self.assertIn("goods_picked_a_version", verdict["reasons"])
        same_price = self.grade([*self.figures(b_price="2.50"), goods],
                                served={"h-a": PI_A, "h-b": PI_B.replace(b"2.75", b"2.50")},
                                proformas=("pin-a",))
        self.assertEqual(same_price["status"], "disagree", same_price)
        self.assertIn("goods_picked_beside_an_unread_amount", same_price["reasons"])

    def test_unknown_goods_with_the_unread_gap_and_version_conflict_agree(self):
        goods = {"name": "line_1_goods", "kind": "computed", "value": None}
        figures = self.figures()
        conflict = {"subject": "PI_VERSION_UNRESOLVED: goods",
                    "values": [{"value": f["value"], "citation": f["citation"]}
                               for f in figures if f["property"] in ("amount", "unit_price")]}
        gap = {"subject": "goods", "reason": oracle.PI_UNREAD}
        verdict = self.grade([*figures, goods], gaps=[gap], conflicts=[conflict])
        self.assertEqual((verdict["status"], verdict["reasons"]), ("agree", []), verdict)
        no_gap = self.grade([*figures, goods], conflicts=[conflict])
        self.assertIn("unread_amount_without_gap", no_gap["reasons"])
        no_conflict = self.grade([*figures, goods], gaps=[gap])
        self.assertIn("goods_unknown_without_version_conflict", no_conflict["reasons"])

    def test_every_confirmation_figure_is_checked_against_its_pinned_bytes(self):
        goods = {"name": "line_1_goods", "kind": "computed", "value": None}
        figures = [f for f in self.figures() if f["object_id"] == "sc-a"]
        clean = self.grade([*figures, goods], proformas=("pin-a",))
        self.assertNotIn("cited_value_not_in_bytes", clean["reasons"])
        for prop, value in (("unit_price", "2.60"), ("quantity", "1200")):
            with self.subTest(prop=prop):   # a figure the pinned bytes do not state
                changed = [dict(f, value=value) if f["property"] == prop else f
                           for f in figures]
                verdict = self.grade([*changed, goods], proformas=("pin-a",))
                self.assertEqual(verdict["status"], "disagree")
                self.assertIn("cited_value_not_in_bytes", verdict["reasons"])
            with self.subTest(prop=prop, bytes="altered"):   # cited from unpinned bytes
                cited = [dict(f, citation={"l1_handle": "h-x", "field_path": prop})
                         if f["property"] == prop else f for f in figures]
                verdict = self.grade([*cited, goods], proformas=("pin-a",),
                                     served={"h-a": PI_A, "h-x": PI_A + b"\n"})
                self.assertIn("cited_bytes_not_pinned", verdict["reasons"])
        unread = self.grade([*figures, goods], proformas=("pin-a",),
                            text_of=lambda _data: None)
        self.assertIn("cited_text_unreadable", unread["reasons"])
        no_reader = self.grade([*figures, goods], proformas=("pin-a",), text_of=None)
        self.assertIn("cited_text_unreadable", no_reader["reasons"])

    def test_unknown_goods_block_only_on_a_typed_reason_the_goods_entry_names(self):
        figures = [f for f in self.figures() if f["object_id"] == "sc-a"]
        goods = {"name": "line_1_goods", "kind": "computed", "value": None,
                 "steps": ["sc-a_amount: 2500.00 USD", "CONFIRMATION_CURRENCY_DIFFERS: the "
                           "confirmed amount carries no cited currency equal to the PO's"]}
        gap = {"subject": "goods of landed_cost_line_1",
               "reason": "confirmation_currency_differs", "who_can_answer": ["PROC"]}
        blocked = self.grade([*figures, goods], gaps=[gap], proformas=("pin-a",))
        self.assertEqual((blocked["status"], blocked["missing_codes"]),
                         ("missing", ["confirmation_currency_differs"]), blocked)
        self.assertIsNone(blocked["missing"][0]["pin"])
        # no typed reason, or a gap the goods entry does not name: still a failure
        for gaps, steps in (([], goods["steps"]), ([gap], goods["steps"][:1]),
                            ([dict(gap, subject="goods")], goods["steps"][:1]),
                            ([dict(gap, subject="freight of landed_cost_line_1")],
                             goods["steps"]),
                            ([dict(gap, reason="Not a code!")], goods["steps"])):
            with self.subTest(gaps=gaps, steps=steps):
                verdict = self.grade([*figures, dict(goods, steps=steps)], gaps=gaps,
                                     proformas=("pin-a",))
                self.assertEqual(verdict["status"], "disagree", verdict)
                self.assertIn("goods_unknown_without_version_conflict", verdict["reasons"])
        # a stated value beside a differing confirmation still disagrees, typed gap or not
        picked = self.grade([*self.figures("2500.00"), dict(goods, value="2500.00")],
                            gaps=[gap])
        self.assertEqual(picked["status"], "disagree")

    def test_comma_decimal_amounts_are_found_in_the_cited_text(self):
        euro = PI_A.replace(b"2,500.00", b"2.500,00")
        figures = [f for f in self.figures() if f["object_id"] == "sc-a"]
        goods = {"name": "line_1_goods", "kind": "computed", "value": "2500.00"}
        verdict = self.grade([*figures, goods], proformas=("pin-a",), pi_a=euro)
        self.assertEqual((verdict["status"], verdict["reasons"]), ("agree", []), verdict)
        # a figure the comma-decimal text does not state is still caught
        other = self.grade([*figures, goods], proformas=("pin-a",),
                           pi_a=euro.replace(b"2.500,00", b"2.600,00"))
        self.assertIn("cited_value_not_in_bytes", other["reasons"])

    def test_a_proforma_unread_for_want_of_the_finance_grant_is_typed(self):
        verdict = self.grade([], held=())
        self.assertEqual((verdict["status"], verdict["missing_codes"]), ("disagree", []))
        self.assertIn(oracle.FINANCE_NOT_GRANTED, verdict["reasons"])
        linked = self.grade([], held=None)
        self.assertEqual(linked["missing_codes"], [oracle.PI_NOT_LINKED])


class MarkingGrantArgumentTests(unittest.TestCase):
    """``--marking-grant MARKING=PRINCIPAL`` is checked as the host config's grants are."""

    def parse(self, *grants: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        add_host_arguments(parser)
        argv = ["--state-dir", "s", "--source-mirror-audience", "a"]
        for grant in grants:
            argv += ["--marking-grant", grant]
        with contextlib.redirect_stderr(io.StringIO()):
            return parser.parse_args(argv)

    def test_valid_grants_merge_and_malformed_ones_are_refused(self):
        args = self.parse("finance=svc:verifier", "finance=staff:ops")
        self.assertEqual(_marking_grants(args.marking_grant),
                         {"finance": ["svc:verifier", "staff:ops"]})
        self.assertEqual(_marking_grants(self.parse().marking_grant), {})
        for invalid in ("finance", "finance=", "finance=staff:", "finance=:ops",
                        "finance=staff: ops", "finance=staff:*", "nope=svc:a", "finance=x"):
            with self.subTest(grant=invalid), self.assertRaises(SystemExit):
                self.parse(invalid)


if __name__ == "__main__":
    unittest.main()
