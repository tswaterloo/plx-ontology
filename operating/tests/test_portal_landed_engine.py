"""Golden tests: the Workbench port of the Portal's landed-cost / quote engine.

Evidence label: ``isolated-fixture``. The expected values are not hand-derived: they are the
outputs of the Portal's own TypeScript (petralabx/plx-customer-portal staging @d4d1bb2), run
stand-alone in Node with a fake
read-only Prisma client (every write refused, ``persist: false``) that answers the Portal's own
queries over the SYNTHETIC records of a scenarios file (``portal_engine/records.mjs``):
``portal_engine/run.mjs`` over ``fixtures/portal_landed_quote_scenarios.json`` (the engine,
captured in ``fixtures/portal_landed_quote_golden.json``) and ``portal_engine/labour.mjs`` over
``fixtures/landed_labour_scenarios.json`` (labour hours, formula and ASSEMBLY BOM selection,
raw-material cost; ``fixtures/landed_labour_golden.json``). Every number must match bit for bit.
The ``test_golden_is_current`` tests re-run the harnesses when Node and the Portal checkout are
present.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from operating import landed_cost as lc
from operating import pallet_build as pb

FIXTURES = Path(__file__).parent / "fixtures"
HARNESS = Path(__file__).parent / "portal_engine"
SCENARIOS = json.loads((FIXTURES / "portal_landed_quote_scenarios.json").read_text("utf-8"))
GOLDEN = json.loads((FIXTURES / "portal_landed_quote_golden.json").read_text("utf-8"))
PORTAL = Path(os.environ.get("PLX_PORTAL_CHECKOUT", "/home/user/plx-customer-portal"))
F = "fixture://portal-landed-quote/"

CALLS = {
    "landedComponentCost": lambda a: lc.landed_component_cost(
        a.get("tierPrice"), a.get("freight"), duty=a.get("duty"), duty_rate=a.get("dutyRate"),
        multiplier=a.get("multiplier")),
    "fillMassKg": lambda a: lc.fill_mass_kg(a["fillWeightG"], a["specificGravity"]),
    "estimateUnitCost": lc.estimate_unit_cost,
    "resolveMarginLadder": lambda a: lc.resolve_margin_ladder(a.get("settings"),
                                                              a.get("quoteOverride")),
    "priceMarginTiers": lambda a: lc.price_margin_tiers(
        a["cost"], lc.resolve_margin_ladder(a["ladder"].get("settings"))),
    "deriveUptimeOverhead": lambda a: lc.derive_uptime_overhead(
        a["hourlyRate"], a.get("assemblyHoursPerUnit"), a.get("assemblyHoursSource"),
        a.get("expectedUnitsPerShift"), a.get("uptimeHours")),
    "pickRunVolumeTierForQty": lambda a: lc.pick_run_volume_tier(a["qty"]),
    "pickSupplierCardTierPrice": lambda a: lc.pick_supplier_card_tier_price(
        a["card"], a["componentQty"], a["runVolumeTierCode"]),
    "allocatePoFreightPerUnit": lambda a: lc.allocate_po_freight_per_unit(
        a["shippingCost"], a["poLines"], a["lineId"]),
    "deriveReadyToQuote": lc.derive_ready_to_quote,
    "rollupBomTotal": lambda a: lc.rollup_bom_total(a["lines"]),
    "computeQuoteTotals": lambda a: lc.compute_quote_totals(a["lineTotals"], a["taxRatePercent"]),
    "deriveUnitPrice": lambda a: lc.derive_unit_price(a["stdCost"], a["marginPercent"]),
    "resolveUnitPrice": lc.resolve_unit_price,
    "lineTotal": lambda a: lc.line_total(a["unitPrice"], a["quantity"]),
    "fallbackMatrixUnitCost": lambda a: lc.fallback_matrix_unit_cost(
        a["engineUnitCost"], a.get("lineStdCost"), a.get("theoreticalFgCost"),
        a.get("customerSuppliedCount")),
    "applyColumnUnitCostFallback": lambda a: lc.apply_column_unit_cost_fallback(
        a["column"], a["lineStdCost"], a["theoreticalFgCost"]),
    "buildLandedExpenseLines": lambda a: lc.build_landed_expense_lines(
        a["productTypes"], a.get("freightAmount"), a.get("dutyAmount")),
    "resolvePostedLandedAmounts": lambda a: lc.resolve_posted_landed_amounts(
        a.get("receiptFreight"), a.get("receiptDuty"), a.get("poFreight"), a.get("poDuty")),
    "customerSuppliedRequirements": lambda a: lc.customer_supplied_requirements(
        a["materials"], a["units"]),
}


def scenario(name: str) -> dict:
    return next(s for s in SCENARIOS["engine"] if s["name"] == name)


# fact key -> the Prisma model whose records the Portal reads for it
RELATIONS = {"project_subassemblies": "projectSubAssembly", "fg_items": "fgItem",
             "formulations": "formulation", "boms": "billOfMaterials", "fg_routings": "fgRouting",
             "draft_cost_overrides": "draftCostOverride",
             "setup_standards": "lineActionSetupStandard",
             # G9: the supply-source read (lc.SUPPLY_RELATIONS)
             "customers": "customer", "products": "product",
             "sourcing_requests": "assemblySourcingRequest",
             "customer_supplied_items": "customerSuppliedItem"}


def project_ref(db: dict, ref: str) -> dict:
    """``resolveQuoteProjectRef``: the gold project by id or key, else the local project."""
    gold = next((p for p in db.get("project", []) if ref in (p["id"], p["projectKey"])), None)
    if gold:
        return dict(gold, goldId=gold["id"])
    local = next(p for p in db.get("projectLocal", []) if p["id"] == ref)
    return {"id": local["id"], "projectKey": local["id"], "customerId": local.get("customerId"),
            "goldId": None, "_source": local["_source"]}


def records_facts(db: dict, project: dict, fail: list | tuple = ()) -> dict:
    """The record lists the Portal's queries select among; a failed read is None (unread)."""
    facts = {key: None if model in fail else db.get(model, [])
             for key, model in RELATIONS.items()}
    return dict(facts, project=project)


def facts_of(sc: dict) -> dict:
    """The records the Portal reads for this scenario (the fake database's tables)."""
    db = sc["db"]
    local = (db.get("projectLocal") or [None])[0]
    facts = records_facts(db, project_ref(db, sc["project_ref"]))
    facts.update({
        "rate_settings": (db.get("rateSettings") or [None])[0],
        "quote_run": {"quantity": db["quoteRunQty"], "_source": F + "quote/latest"}
        if db.get("quoteRunQty") is not None else None,
        "project_core": {"target_price": local.get("targetPrice"),
                         "customer_id": local.get("customerId"), "_source": local["_source"]}
        if local else None,
        "purchase_order_lines": db.get("purchaseOrderLine") or [],
        "inventory": db.get("inventory") or []})
    return facts


class GoldenFunctionsTest(unittest.TestCase):
    def test_every_function_vector_matches_the_portal_bit_for_bit(self):
        self.assertEqual(set(GOLDEN["functions"]), set(CALLS))
        for name, cases in GOLDEN["functions"].items():
            for case in cases:
                with self.subTest(function=name, args=case["args"]):
                    self.assertEqual(CALLS[name](case["args"]), case["result"])

    def test_rounding_ties_follow_math_round_on_doubles(self):
        # 0.13 × 0.065 is 0.008449999… as a double; Decimal half-up would give 0.0085 from
        # 0.00845 exactly. The Portal (and the port) round the double.
        [case] = [c for c in GOLDEN["functions"]["landedComponentCost"]
                  if c["args"] == {"tierPrice": 0.13, "freight": 0.02}]
        self.assertEqual(lc.landed_component_cost(0.13, 0.02)["duty"], case["result"]["duty"])
        self.assertEqual(lc.js_string(0.00005), "0.00005")
        self.assertEqual(lc.js_string(1e21), "1e+21")
        self.assertEqual(lc.js_string(25.0), "25")
        self.assertEqual(lc.portal_decimal(0.1 + 0.2), Decimal("0.30000000000000004"))

    def test_js_number_follows_string_to_number(self):
        for text, expected in (("1_000", None), ("inf", None), ("Inf", None), ("nan", None),
                               ("NaN", None), ("1,5", None), (" 12.5 ", 12.5), ("", 0.0),
                               (".5", 0.5), ("5.", 5.0), ("-1e3", -1000.0), ("0x10", 16.0),
                               ("-0x10", None), ("Infinity", float("inf")),
                               ("-Infinity", float("-inf")), ("infinity", None)):
            self.assertEqual(lc.js_number(text), expected, text)


def _node_version() -> tuple[int, ...] | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20,
                             env={"PATH": os.environ.get("PATH", "")}).stdout
        return tuple(int(x) for x in out.strip().lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _pinned_files_moved() -> str | None:
    for rel, digest in GOLDEN["portal_src_files"].items():
        path = PORTAL / "portal" / rel.removeprefix("portal/")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return f"Portal checkout moved past the pinned {rel}; re-pin the golden"
    return None


def _harness_skip_reason() -> str | None:
    if not (PORTAL / "portal/src").is_dir():
        return "Portal checkout not present"
    version = _node_version()
    if version is None or version < (22, 6):
        return "Node >= 22.6 (--experimental-transform-types) not present"
    return _pinned_files_moved()


class GoldenEngineTest(unittest.TestCase):
    PORTED = ("estimate", "fillWeightG", "specificGravity", "fillMassKg", "bomTotal",
              "resolutionSource", "labour", "labourIsPlaceholder", "marginLadder", "marginTiers",
              "overhead", "census", "lineProvenance")
    COLUMN = ("askedUnits", "labour", "lines", "estimate", "unitCost", "marginTiers", "overhead")
    COMPONENT = ("lineKey", "lineName", "qtyPerUnit", "componentDemand", "runVolumeTierCode",
                 "unitCost", "unitCostSource", "supplySource")

    def test_quote_project_landed_matches_for_every_scenario(self):
        for sc in SCENARIOS["engine"]:
            expected = GOLDEN["scenarios"][sc["name"]]["result"]
            got = lc.portal_quote_landed(facts_of(sc), asked_volumes=sc.get("asked_volumes"))
            with self.subTest(scenario=sc["name"]):
                for key in self.PORTED:
                    self.assertEqual(got[key], expected[key], key)
                # G9: present only when the customer supplies something
                self.assertEqual(got.get("customerSupplied"), expected.get("customerSupplied"))
                self.assertEqual(len(got["volumeMatrix"]), len(expected["volumeMatrix"]))
                for mine, theirs in zip(got["volumeMatrix"], expected["volumeMatrix"]):
                    for key in self.COLUMN:
                        self.assertEqual(mine[key], theirs[key], (mine["askedUnits"], key))
                    self.assertEqual(mine.get("customerSupplied"), theirs.get("customerSupplied"))
                    self.assertEqual(
                        [{k: c[k] for k in self.COMPONENT} for c in mine["componentLines"]],
                        theirs["componentLines"])

    def test_saved_offer_and_quote_totals_match(self):
        for sc in SCENARIOS["engine"]:
            expected = GOLDEN["scenarios"][sc["name"]]["offer"]
            if sc.get("offer") is None:
                continue
            got = lc.portal_quote_landed(facts_of(sc), asked_volumes=sc.get("asked_volumes"))
            std = lc.first_positive_line_std_cost(sc["offer"]["quoteLines"])
            priced = [lc.apply_column_unit_cost_fallback(col, std,
                                                         got["estimate"]["totalCostPerUnit"])
                      for col in got["volumeMatrix"]]
            lines = []
            for item in priced:
                column = item["column"]
                margin = lc.resolve_offer_margin_pct(column, sc["offer"]["selections"],
                                                     sc["offer"]["quoteMarginTargetPct"])
                lines.append(lc.offer_line(column["askedUnits"], column["unitCost"], margin,
                                           lc.price_column_at_margin(column, margin)))
            with self.subTest(scenario=sc["name"]):
                self.assertEqual(std, expected["lineStdCost"])
                self.assertEqual([p["source"] for p in priced], expected["sources"])
                self.assertEqual([{k: x[k] for k in ("quantity", "unitPrice", "totalPrice",
                                                      "marginPercent")} for x in lines],
                                 expected["creates"])
                self.assertEqual([x["stdCost"] for x in lines],
                                 [x["unitCost"] for x in expected["lines"]])
                self.assertEqual(lc.compute_quote_totals([x["totalPrice"] for x in lines],
                                                         sc["offer"]["taxRatePercent"]),
                                 expected["totals"])

    def test_the_harness_never_wrote(self):
        for name, run in GOLDEN["scenarios"].items():
            for call in run["db_calls"]:
                self.assertRegex(call, r"\.(findFirst|findUnique|findMany)$|^\$queryRaw", name)

    def test_golden_pins_the_cited_portal_files(self):
        self.assertEqual(GOLDEN["portal_commit"], lc.PORTAL_REVISION)
        for source in lc.PORTAL_SOURCES.values():
            for part in source.split("; "):
                self.assertIn("@" + lc.PORTAL_SHORT, part)

    def test_golden_is_current(self):
        """Re-run the harness against the checkout. Skipped (like the cited-lines test) without
        Node >= 22.6 or the checkout, or when the checkout moved past a pinned file: a moved
        Portal is re-pinned deliberately, not failed in an unrelated run."""
        skip = _harness_skip_reason()
        if skip:
            self.skipTest(skip)
        src = PORTAL / "portal/src"
        env = {"PATH": os.environ.get("PATH", ""), "TZ": "UTC", "PORTAL_SRC": str(src),
               "PORTAL_COMMIT": GOLDEN["portal_commit"]}
        out = subprocess.run(
            ["node", "--experimental-transform-types", "--no-warnings", "--import",
             "./register.mjs", "run.mjs", str(FIXTURES / "portal_landed_quote_scenarios.json")],
            cwd=HARNESS, env=env, capture_output=True, text=True, timeout=120, check=True)
        self.assertEqual(json.loads(out.stdout), GOLDEN)


# ---- labour hours, formula / ASSEMBLY BOM selection and raw cost (LOG-08) ---------------------

LABOUR_SCENARIOS = json.loads((FIXTURES / "landed_labour_scenarios.json").read_text("utf-8"))
LABOUR_GOLDEN = json.loads((FIXTURES / "landed_labour_golden.json").read_text("utf-8"))


def _attempt(call):
    try:
        return call()
    except lc.LandedCostError as err:
        return {"error": str(err)}


LABOUR_CALLS = {
    "quoteLabourFromRouting": lambda a, r: lc.quote_labour_from_routing(
        a["routing"], a.get("assemblyRate"), (a.get("opts") or {}).get("runQty")),
    "assemblyLabourPerUnit": lambda a, r: _attempt(
        lambda: lc.assembly_labour_per_unit(a["headcount"], a["rateBpm"])),
    "compoundingLabourPerUnit": lambda a, r: _attempt(lambda: lc.compounding_labour_per_unit(
        a["compounderCount"], a["hours"], a["yieldUnits"])),
    "amortiseSetupHours": lambda a, r: lc.amortise_setup_hours(
        a["setupMinutes"], a["changeoverMinutes"], a["charges"], a["askedUnits"], a.get("crew")),
    "compoundingCrewHoursPerUnit": lambda a, r: lc.compounding_crew_hours_per_unit(
        a["compounderCount"], a["hoursPerCharge"], a["charges"], a["askedUnits"]),
    "resolveSetupStandard": lambda a, r: lc.resolve_setup_standard(
        a["rows"], line_number=a["query"].get("lineNumber"),
        group_code=a["query"].get("groupCode"), action_keys=a["query"].get("actionKeys")),
    "resolveQuoteHours": lambda a, r: lc.resolve_quote_hours(
        a["projectKey"], a["drafts"], a["routing"], a["runQty"], a["fgTargetQty"],
        a["assemblyRate"]),
    "resolveExpectedUnitsOverride": lambda a, r: lc.resolve_expected_units_override(
        a["drafts"], a["projectKey"], a["pricedTierCode"]),
    # the vessel plan is an input of the port: the Portal's own charge count is passed in
    "buildVolumeMatrixColumn": lambda a, r: dict(lc.volume_column_labour(
        a["askedUnits"], a["projectKey"], a["drafts"], a["routing"], a["setupStandard"],
        r["charges"], a["assemblyRate"], a["rates"]), charges=r["charges"]),
    "rawCostPerKg": lambda a, r: lc.raw_cost_per_kg(a),
    "rollupBomTotal": lambda a, r: lc.rollup_bom_total(a["lines"]),
}


def _without_source(rows):
    return [{k: v for k, v in row.items() if k != "_source"} for row in rows]


class LabourGoldenTest(unittest.TestCase):
    """LOG-08: the labour-hour derivation (routing labour, crew/shift and headcount/BPM draft
    tiers, setup amortisation and standards), the formula resolution order, the ASSEMBLY BOM
    selection, the BOM rollup and rawCostPerKg's three branches, against the Portal."""

    def test_every_function_vector_matches_the_portal(self):
        self.assertEqual(LABOUR_GOLDEN["portal_commit"], lc.PORTAL_REVISION)
        self.assertEqual(set(LABOUR_GOLDEN["functions"]), set(LABOUR_CALLS))
        for name, cases in LABOUR_GOLDEN["functions"].items():
            for case in cases:
                with self.subTest(function=name, args=case["args"]):
                    self.assertEqual(LABOUR_CALLS[name](case["args"], case["result"]),
                                     case["result"])

    def test_the_portal_queries_select_the_same_records(self):
        for sc in LABOUR_SCENARIOS["records"]:
            golden = LABOUR_GOLDEN["records"][sc["name"]]["results"]
            fail = sc.get("fail", [])
            for call in LABOUR_GOLDEN["records"][sc["name"]]["db_calls"]:  # persist: false
                self.assertRegex(call, r"\.(findFirst|findUnique|findMany)$|^\$queryRaw",
                                 sc["name"])
            for name, expected in golden.items():
                with self.subTest(scenario=sc["name"], read=name):
                    self.assertNotIn("error", expected or {})
                    self.assertEqual(self._read(name, sc, fail, expected), expected)

    def _read(self, name, sc, fail, expected):
        db, args = sc["db"], sc["calls"][name]
        if name == "resolveQuoteFormulation":
            facts = records_facts(db, {}, fail)
            got = lc.resolve_quote_formulation(
                args["projectKey"], args["projectId"], facts["project_subassemblies"],
                facts["fg_items"], facts["formulations"])
            formulation = got["formulation"] or {}
            return {"source": got["source"], "formulationId": formulation.get("id"),
                    "rawCostPerKg": [lc.quote_raw_cost_per_kg(i["product"])
                                     for i in formulation.get("ingredients") or []]}
        if name == "gatherQuoteFacts":
            project = expected["project"]
            q = lc.quote_facts(records_facts(db, project, fail))
            return {"project": project, "resolutionSource": q["resolutionSource"],
                    "formulationId": (q["formulation"] or {}).get("id"),
                    "specificGravity": q["specificGravity"], "fillWeightG": q["fillWeightG"],
                    "bomTotal": q["bomTotal"], "rawMaterials": q["rawMaterials"],
                    "componentLines": [{k: line[k] for k in ("lineKey", "lineName", "qtyPerUnit",
                                                             "linked", "pricedUnitCost")}
                                       for line in q["componentLines"]],
                    "fgTargetQty": q["fgTargetQty"],
                    "unsourcedLineCount": q["unsourcedLineCount"],
                    "linkedLineCount": q["linkedLineCount"]}
        facts = records_facts(db, {}, fail)
        if name == "getPrimaryRouting":
            routing = lc.primary_routing(facts["fg_routings"], args, facts["setup_standards"])[0]
            return routing and {k: routing[k] for k in (
                "id", "standardHoursPerUnit", "standardSetupMinutes", "standardChangeoverMinutes",
                "totalOperators")}
        return _without_source(lc.draft_cost_rows(facts["draft_cost_overrides"], args))

    def test_golden_is_current(self):
        """Re-run labour.mjs on the checkout; skipped without Node >= 22.6, the checkout or
        when a pinned file moved (a moved Portal is re-pinned deliberately)."""
        if not (PORTAL / "portal/src").is_dir():
            self.skipTest("Portal checkout not present")
        version = _node_version()
        if version is None or version < (22, 6):
            self.skipTest("Node >= 22.6 (--experimental-transform-types) not present")
        for rel, digest in LABOUR_GOLDEN["portal_src_files"].items():
            path = PORTAL / rel
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                self.skipTest(f"Portal checkout moved past the pinned {rel}; re-pin the golden")
        env = {"PATH": os.environ.get("PATH", ""), "TZ": "UTC",
               "PORTAL_SRC": str(PORTAL / "portal/src"),
               "PORTAL_COMMIT": LABOUR_GOLDEN["portal_commit"]}
        out = subprocess.run(
            ["node", "--experimental-transform-types", "--no-warnings", "--import",
             "./register.mjs", "labour.mjs", str(FIXTURES / "landed_labour_scenarios.json")],
            cwd=HARNESS, env=env, capture_output=True, text=True, timeout=120, check=True)
        self.assertEqual(json.loads(out.stdout), LABOUR_GOLDEN)


class CitedPortalLinesTest(unittest.TestCase):
    """The engine cites exact lines at d4d1bb2."""

    @unittest.skipUnless((PORTAL / "portal/src").is_dir(), "Portal checkout not present")
    def test_cited_lines_hold_in_the_checkout(self):
        moved = _pinned_files_moved()
        if moved:
            self.skipTest(moved)
        read = lambda rel: (PORTAL / rel).read_text("utf-8").splitlines()  # noqa: E731
        engine = read("portal/src/lib/costing/project-landed-quote.ts")
        self.assertIn("landedComponentCost({", engine[823 - 1])
        self.assertIn("landedComponentCost({", engine[1881 - 1])
        self.assertIn("costPerKg: rawCostPerKg(ing.product, supplySource)", engine[739 - 1])
        self.assertIn("async function findAssemblyBomForQuote", engine[571 - 1])
        self.assertIn("export function resolveLineBasis", engine[1030 - 1])
        self.assertIn("export function resolveQuoteHours", engine[1146 - 1])
        self.assertIn("export function buildVolumeMatrixColumn", engine[1735 - 1])
        self.assertIn("export function priceQuoteInputs", engine[2092 - 1])
        # pricing reads the filled setup minutes and never saves them (CT-B, RV2a, CT-A)
        inputs = read("portal/src/lib/costing/quote-inputs.ts")
        self.assertIn("export async function loadQuoteInputs", inputs[111 - 1])
        self.assertIn("getPrimaryRouting(projectKey, { persist: false, setupStandardRows })",
                      inputs[99 - 1])
        self.assertIn('source: "rate_settings"', inputs[151 - 1])
        costing = "portal/src/lib/costing/"
        for rel, line, text in (
                ("fg-routing.ts", 137, "export function quoteLabourFromRouting"),
                ("template-cost-matrix.ts", 84, "export function compoundingLabourPerUnit"),
                ("template-cost-matrix.ts", 96, "export function assemblyLabourPerUnit"),
                ("setup-standards.ts", 138, "export function resolveSetupStandard"),
                ("setup-standards.ts", 208, "export function amortiseSetupHours"),
                ("setup-standards.ts", 259, "export function compoundingCrewHoursPerUnit"),
                ("fg-routing-store.ts", 274, "async function withSetupFilled"),
                ("setup-standards-store.ts", 104, "export async function loadSetupStandardRows"),
                ("draft-cost-overrides.ts", 420, "export function resolveDraftCost"),
                ("formulation-resolution.ts", 150, "export async function resolveQuoteFormulation"),
                ("raw-cost.ts", 49, "export function rawCostPerKg"),
                ("../customer-materials/supply-source-shared.ts", 76,
                 "export function supplySourceFromFacts"),
                ("../customer-materials/supply-source.ts", 152,
                 "export async function resolveSupplySources"),
                ("../customers/anchor.ts", 99, "export async function findCustomerAnchor"),
                ("bom-total.ts", 14, "export function rollupBomTotal"),
                ("../assembly-bom/draft-cost-persist.ts", 113, "function toDraftRow"),
                ("ready-to-quote.ts", 461, "assembly and setup labour are priced at $0")):
            self.assertIn(text, read(costing + rel)[line - 1], rel)
        self.assertIn("landedComponentCost({", read("portal/src/lib/quote/matrix.ts")[111 - 1])
        self.assertIn("landedComponentCost({", read("portal/src/lib/quote/workbook.ts")[208 - 1])
        self.assertIn("landedComponentCost({",
                      read("portal/src/lib/costing/quote-advisories.ts")[407 - 1])
        self.assertIn("export function landedComponentCost",
                      read("portal/src/lib/costing/landed-cost.ts")[47 - 1])
        self.assertIn("ERP-D-015", read(
            "docs/projects/business-central-erp-modernization/decision-log.md")[25 - 1])
        gl = read("portal/src/lib/procurement/landed-gl.ts")
        self.assertIn("export const LANDED_GL", gl[8 - 1])
        self.assertIn("export function buildLandedExpenseLines", gl[57 - 1])
        self.assertIn("export function resolvePostedLandedAmounts", gl[88 - 1])
        compat = read("scripts/migrations/20260327_2205_fm_snake_case_compat.sql")
        self.assertEqual((compat[59 - 1].strip(), compat[60 - 1].strip()),
                         ("('receiptraw', 'receipt_raw_material'),",
                          "('receiptpack', 'receipt_packaging'),"))
        self.assertIn("async function resolvePurchaseOrderId",
                      read("portal/src/lib/mrp/upsert-gold-receipt.ts")[117 - 1])
        self.assertIn('"staging_reporting"', read("scripts/db-targets.json")[45 - 1])

    @unittest.skipUnless((PORTAL / ".git").exists(), "Portal checkout not present")
    def test_every_cited_range_lies_inside_its_file_at_the_pinned_revision(self):
        """Each ``path@rev#La-b`` names lines that exist at that revision: three ranges cited at
        45673ee pointed past the end of their file (cost-estimate.ts, volume-tiers.ts,
        bom-total.ts) and nothing caught it."""
        lengths: dict[str, int] = {}
        for name, source in lc.PORTAL_SOURCES.items():
            for part in source.split("; "):
                path, _, lines = part.partition("@" + lc.PORTAL_SHORT + "#L")
                first, _, last = lines.partition("-")
                first, last = int(first), int(last or first)
                if path not in lengths:
                    shown = subprocess.run(
                        ["git", "-C", str(PORTAL), "show", f"{lc.PORTAL_REVISION}:{path}"],
                        capture_output=True, text=True, timeout=60,
                        env={"PATH": os.environ.get("PATH", "")})
                    if shown.returncode != 0:
                        self.skipTest(f"{lc.PORTAL_SHORT} not in the checkout")
                    lengths[path] = len(shown.stdout.splitlines())
                with self.subTest(source=name, part=part):
                    self.assertTrue(1 <= first <= last <= lengths[path], lengths[path])


class WorkbenchLayerTest(unittest.TestCase):
    def landed(self, name, **kwargs):
        sc = scenario(name)
        args = {"quote_currency": "USD",
                "cost_currency": {"currency": "USD", "_source": F + "finance/house-currency"}}
        args.update(kwargs)
        return lc.quote_landed(facts_of(sc), asked_volumes=sc.get("asked_volumes"), **args)

    def test_unknowns_are_typed_and_the_portal_figure_is_not_stated_as_landed_cost(self):
        out = self.landed("linked_cards_routed")
        codes = [u["code"] for u in out["unknowns"]]
        self.assertEqual(out["status"], "unknown")
        self.assertIsNone(out["workbench"])
        # the Portal figure is kept, labelled, for comparison
        self.assertEqual(out["portal"]["estimate"]["totalCostPerUnit"],
                         GOLDEN["scenarios"]["linked_cards_routed"]["result"]["estimate"][
                             "totalCostPerUnit"])
        self.assertIn("not Portal output and not item cost", out["label"])
        self.assertEqual(codes.count("TARIFF_TREATMENT_UNVERIFIED"), 3)
        by_subject = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("FREIGHT_PER_COMPONENT_MISSING", "packaging line cap"), by_subject)
        self.assertIn(("INCOTERMS_MISSING", "packaging line cap"), by_subject)
        self.assertIn(("ORIGIN_MISSING", "packaging line shipper case"), by_subject)
        self.assertIn(("FX_NOT_APPLIED", "packaging line shipper case"), by_subject)
        self.assertIn(("SELLING_UNIT_NOT_COMPONENT", "packaging line shipper case"), by_subject)
        self.assertIn(("PACKAGING_QTY_DEFAULTED", "packaging line shipper case"), by_subject)
        freight = next(u for u in out["unknowns"] if u["code"] == "FREIGHT_PER_COMPONENT_MISSING")
        self.assertEqual(freight["portal_used"], 0)
        self.assertEqual(freight["evidence"], [F + "supplier_product/sp-cap"])
        self.assertNotIn("INPUT_NOT_CITED", codes)
        self.assertIn(F + "rate_settings/1", out["inputs_cited"])
        self.assertEqual(out["cost_treatment"]["kind"], "analytic_estimate_not_item_cost")

    def test_missing_facts_propagate_as_unknowns_never_as_zero(self):
        out = self.landed("project_without_facts")
        codes = {u["code"] for u in out["unknowns"]}
        self.assertLessEqual({"FORMULA_NOT_PRICED", "FILL_WEIGHT_MISSING", "PACKAGING_BOM_MISSING",
                              "RATE_SETTINGS_DEFAULTED",
                              "LABOUR_HOURS_MISSING", "OVERHEAD_BASIS_MISSING",
                              "SPECIFIC_GRAVITY_DEFAULTED"}, codes)
        self.assertEqual(out["portal"]["estimate"]["totalCostPerUnit"], 0)  # what Portal shows
        self.assertEqual((out["status"], out["workbench"]), ("unknown", None))
        out = self.landed("unsourced_lines_fg_pin")
        by = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("PACKAGING_LINE_UNSOURCED", "packaging line label"), by)
        self.assertIn(("PACKAGING_LINE_UNSOURCED", "packaging line jar"), by)
        self.assertIn(("SUPPLIER_CARD_MISSING", "packaging line lid"), by)
        self.assertIn(("RAW_MATERIAL_COST_MISSING", "base"), by)
        self.assertIn(("RAW_MATERIAL_COST_UNIT_UNVERIFIED", "base"), by)

    def test_currency_must_be_declared_and_cited(self):
        out = self.landed("linked_cards_routed", cost_currency=None)
        self.assertIn("COST_CURRENCY_UNDECLARED", {u["code"] for u in out["unknowns"]})
        out = self.landed("linked_cards_routed", quote_currency="CAD")
        fx = [u for u in out["unknowns"] if u["code"] == "FX_NOT_APPLIED"]
        self.assertTrue(any(u["subject"] == "quote" for u in fx))
        with self.assertRaises(lc.LandedCostError):
            self.landed("linked_cards_routed", quote_currency="DOLLARS")

    def test_fully_evidenced_quote_is_computed_with_decimal_units(self):
        sc = json.loads(json.dumps(scenario("linked_cards_routed")))
        db = sc["db"]
        lines = db["billOfMaterials"][0]["lines"]
        lines[1]["product"]["supplierProducts"][0].update(deliveryCost=0.004, incoterms="FOB")
        lines[1]["product"]["hsCode"] = "3923.50"
        lines[2]["quantity"] = 1
        lines[2]["product"]["supplierProducts"][0].update(currency="USD", countryOfOrigin="CN",
                                                          uomPerSellingUnit=1)
        tariff = {line["productId"]: {"hs_code": line["product"]["hsCode"], "duty_rate": "0.065",
                                      "_source": F + f"broker/{line['productId']}"}
                  for line in lines if line.get("product")}
        facts = facts_of(sc)
        facts["vessel_plans"] = {units: {"charges": 1, "_source": F + f"vessel_plan/{units}"}
                                 for units in ("5000", "24000", "60000")}
        out = lc.quote_landed(facts, quote_currency="USD",
                              cost_currency={"currency": "USD",
                                             "_source": F + "finance/house-currency"},
                              tariff_evidence=tariff, asked_volumes=sc["asked_volumes"])
        self.assertEqual(out["unknowns"], [])
        self.assertEqual(out["labour"], {"is_placeholder": False,
                                         "basis": [F + "fg_routing/rt-1"]})
        self.assertEqual(out["status"], "computed")
        unit = out["workbench"]["unit_cost"]
        self.assertEqual(unit["unit"], "USD/EA")
        self.assertEqual(Decimal(unit["value"]),
                         lc.portal_decimal(out["portal"]["estimate"]["totalCostPerUnit"]))
        self.assertEqual([c["unit_cost"]["value"] for c in out["workbench"]["columns"]],
                         [lc.js_string(c["unitCost"]) for c in out["portal"]["volumeMatrix"]])
        again = lc.quote_landed(facts, quote_currency="USD",
                                cost_currency={"currency": "USD",
                                               "_source": F + "finance/house-currency"},
                                tariff_evidence=tariff, asked_volumes=sc["asked_volumes"])
        self.assertEqual(out["digest"], again["digest"])
        tariff["p-cap"]["duty_rate"] = "0.25"  # e.g. a Section 301 rate on top
        out = lc.quote_landed(facts, quote_currency="USD",
                              cost_currency={"currency": "USD",
                                             "_source": F + "finance/house-currency"},
                              tariff_evidence=tariff, asked_volumes=sc["asked_volumes"])
        self.assertEqual([u["code"] for u in out["unknowns"]], ["PORTAL_DUTY_RATE_DIFFERS"])

    def test_a_volume_without_hours_is_an_unknown_for_that_column(self):
        out = self.landed("draft_tier_overrides")
        subjects = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("LABOUR_HOURS_MISSING", "assembly hours at 30000 units"), subjects)
        self.assertIn(("OVERHEAD_BASIS_MISSING", "overhead at 30000 units"), subjects)
        self.assertNotIn(("LABOUR_HOURS_MISSING", "assembly hours"), subjects)
        column = out["portal"]["volumeMatrix"][1]
        self.assertEqual((column["askedUnits"], column["overhead"]["perUnit"]), (30000, None))

    def test_code_default_rates_are_unknowns_not_a_computed_cost(self):
        out = self.landed("project_without_facts")
        self.assertIn("RATE_SETTINGS_DEFAULTED", {u["code"] for u in out["unknowns"]})
        sc = json.loads(json.dumps(scenario("linked_cards_routed")))
        sc["db"]["rateSettings"][0]["batchingQuotingLaborRate"] = None
        out = lc.quote_landed(facts_of(sc), quote_currency="USD",
                              cost_currency={"currency": "USD", "_source": F + "fin"},
                              asked_volumes=sc["asked_volumes"])
        self.assertIn(("RATE_SETTING_DEFAULTED", "RateSettings.batchingQuotingLaborRate"),
                      {(u["code"], u["subject"]) for u in out["unknowns"]})

    def test_an_uncited_input_is_an_unknown(self):
        sc = json.loads(json.dumps(scenario("linked_cards_routed")))
        del sc["db"]["billOfMaterials"][0]["lines"][0]["product"]["supplierProducts"][1]["_source"]
        out = lc.quote_landed(facts_of(sc), quote_currency="USD",
                              cost_currency={"currency": "USD", "_source": F + "fin"},
                              asked_volumes=sc["asked_volumes"])
        self.assertIn("INPUT_NOT_CITED", {u["code"] for u in out["unknowns"]})


    def test_labour_is_derived_and_a_placeholder_only_without_a_cited_basis(self):
        """LOG-08: hours come from the cited routing or crew standard (draft tier row); a
        seeded draft row is a template default, not a standard."""
        out = self.landed("linked_cards_routed")
        self.assertFalse(out["portal"]["labourIsPlaceholder"])
        self.assertEqual(out["labour"], {"is_placeholder": False,
                                         "basis": [F + "fg_routing/rt-1"]})
        out = self.landed("card_priced_raw_crew_drafts")
        self.assertEqual(out["portal"]["labour"]["source"], "draft_tier")
        self.assertEqual(out["labour"], {"is_placeholder": False, "basis": [
            F + "draft_cost_override/PK-4-assembly-25k-confirmed",
            F + "draft_cost_override/PK-4-compounding-25k-confirmed"]})
        out = self.landed("draft_tier_overrides")  # source "override" reads as "seed"
        self.assertFalse(out["portal"]["labourIsPlaceholder"])
        self.assertTrue(out["labour"]["is_placeholder"])
        self.assertIn("LABOUR_FROM_SEED_DEFAULTS", {u["code"] for u in out["unknowns"]})
        out = self.landed("project_without_facts")
        self.assertTrue(out["portal"]["labourIsPlaceholder"])
        self.assertEqual(out["labour"], {"is_placeholder": True, "basis": []})
        # 35cfb18 (TASK-2811): unrouted labour is priced at $0 and reported, never as an
        # assumed hour; the census says so, the way the Portal's ready-to-quote.ts does
        census = {i["key"]: i for i in out["portal"]["census"]["items"]}
        self.assertEqual((census["labour"]["severity"], census["labour"]["detail"]),
                         ("warning", "No routing hours, so assembly and setup labour are "
                                     "priced at $0 and reported"))
        estimate = out["portal"]["estimate"]
        self.assertEqual((estimate["assemblyLabourPerUnit"], estimate["productionHoursPerUnit"],
                          estimate["overheadPerUnit"]), (0, 0, 0))
        sc = json.loads(json.dumps(scenario("linked_cards_routed")))
        del sc["db"]["fgRouting"][0]["_source"]
        out = lc.quote_landed(facts_of(sc), quote_currency="USD",
                              cost_currency={"currency": "USD", "_source": F + "fin"},
                              asked_volumes=sc["asked_volumes"])
        self.assertTrue(out["labour"]["is_placeholder"])
        self.assertIn(("INPUT_NOT_CITED", "fg_routing"),
                      {(u["code"], u["subject"]) for u in out["unknowns"]})

    def test_raw_priced_from_the_preferred_card_is_cited_and_its_currency_checked(self):
        out = self.landed("card_priced_raw_crew_drafts")
        by = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("FX_NOT_APPLIED", "new raw"), by)  # systemCost is CAD/kg
        self.assertIn(("RAW_MATERIAL_COST_MISSING", "uncosted"), by)
        self.assertNotIn(("RAW_MATERIAL_COST_MISSING", "new raw"), by)
        self.assertIn(F + "supplier_product/sp-b3", out["inputs_cited"])
        out = self.landed("card_priced_raw_crew_drafts",
                          cost_currency={"currency": "CAD", "_source": F + "fin"})
        self.assertNotIn(("FX_NOT_APPLIED", "new raw"),
                         {(u["code"], u["subject"]) for u in out["unknowns"]})

    def test_an_unread_relation_is_an_unknown_never_an_empty_table(self):
        """A None record list is a failed read: Portal prices it with its fallback and
        lists it in QuoteInputs.readErrors (quote-inputs.ts@d4d1bb2, CT-A); the port
        prices the same fallback and names the relation as RECORDS_NOT_READ.  The rows
        cover the readErrors reads (routing, draft store, FG item, setup standards);
        compounding_standard is not ported (vessel plans arrive as input)."""
        def unknown_codes(o):
            return {u["code"] for u in o["unknowns"]}
        for name, key, check in (
                ("linked_cards_routed", "fg_routings",
                 lambda o: o["portal"]["labour"]["source"] == "missing"),
                ("linked_cards_routed", "project_subassemblies",
                 lambda o: o["portal"]["resolutionSource"] == "pin_unresolved"),
                ("linked_cards_routed", "boms", lambda o: o["portal"]["bomTotal"] == 0),
                # The draft tier falls back to the routing tier, as Portal's empty store.
                ("card_priced_raw_crew_drafts", "draft_cost_overrides",
                 lambda o: o["portal"]["labour"]["assemblySource"] == "routing"),
                ("card_priced_raw_crew_drafts", "setup_standards",
                 lambda o: "SETUP_STANDARD_DEFAULTED" in unknown_codes(o)),
                ("card_priced_raw_crew_drafts", "fg_items",
                 lambda o: o["portal"]["resolutionSource"] == "pin_unresolved")):
            sc = scenario(name)
            baseline = lc.quote_landed(facts_of(sc), quote_currency="USD",
                                       cost_currency={"currency": "USD", "_source": F + "fin"},
                                       asked_volumes=sc["asked_volumes"])
            facts = facts_of(sc)
            facts[key] = None
            out = lc.quote_landed(facts, quote_currency="USD",
                                  cost_currency={"currency": "USD", "_source": F + "fin"},
                                  asked_volumes=sc["asked_volumes"])
            with self.subTest(scenario=name, relation=key):
                # The check is the fallback itself, not a state the read already had.
                self.assertFalse(check(baseline))
                self.assertNotIn(("RECORDS_NOT_READ", key),
                                 {(u["code"], u["subject"]) for u in baseline["unknowns"]})
                self.assertIn(("RECORDS_NOT_READ", key),
                              {(u["code"], u["subject"]) for u in out["unknowns"]})
                self.assertTrue(check(out))
                self.assertEqual(out["status"], "unknown")

    def test_customer_supplied_materials_price_at_zero_and_pending_is_an_unknown(self):
        """G9 (plx-customer-portal 77f1ee6): what the customer supplies costs $0 whatever card
        PLX holds; an open customer-path request is priced $0 too, and stays an open unknown."""
        out = self.landed("customer_supplied_g9")
        sources = {m["key"]: m["supplySource"] for m in out["portal"]["customerSupplied"]}
        # another customer's registry row and request, a stopped row and a closed or
        # VENDOR-path request decide nothing
        self.assertEqual(sources, {"rm-surf": "CUSTOMER", "rm-frag": "CUSTOMER_PENDING",
                                   "rm-trace": "CUSTOMER", "l-bottle": "CUSTOMER",
                                   "l-label": "CUSTOMER_PENDING"})
        pending = {u["subject"]: u["evidence"] for u in out["unknowns"]
                   if u["code"] == "CUSTOMER_SUPPLY_PENDING"}
        self.assertEqual(pending, {
            "customer-supplied raw fragrance": [F + "sourcing_request/sr-4"],
            "customer-supplied packaging label": [F + "sourcing_request/sr-1"]})
        subjects = {u["subject"] for u in out["unknowns"]}
        for name in ("packaging line 1000 ml bottle", "packaging line label", "surfactant"):
            self.assertNotIn(name, subjects)  # no card, tariff or cost checks on what is sent
        self.assertLessEqual({F + "customer_supplied_item/csi-3", F + "customer/cust-g77"},
                             set(out["inputs_cited"]))
        self.assertIn("CUSTOMER_SUPPLIED_AT_ZERO", {a["code"] for a in out["assumptions"]})
        self.assertEqual(out["status"], "unknown")

    def test_an_unread_supply_relation_stops_the_quote(self):
        """The Portal throws SupplySourceReadError rather than price the customer's material:
        an unread supply relation fails closed, never as an empty table (PLX pricing)."""
        for key, relation in lc.SUPPLY_RELATIONS.items():
            facts = facts_of(scenario("customer_supplied_g9"))
            facts[key] = None
            with self.subTest(relation=key), \
                    self.assertRaisesRegex(lc.LandedCostError,
                                           "supply_source_read_failed:" + relation):
                lc.quote_landed(facts, quote_currency="USD", cost_currency=None)
        facts = facts_of(json.loads(json.dumps(scenario("customer_supplied_g9"))))
        facts["boms"][0]["lines"][0]["id"] = 7  # an id that is not text is never asked as PLX
        with self.assertRaisesRegex(lc.LandedCostError, "supply_source_id_must_be_text"):
            lc.quote_landed(facts, quote_currency="USD", cost_currency=None)
        facts = facts_of(scenario("project_without_facts"))  # no line or raw is asked about
        facts.update({key: None for key in lc.SUPPLY_RELATIONS})
        self.assertNotIn("customerSupplied",
                         lc.quote_landed(facts, quote_currency="USD", cost_currency=None)["portal"])

    def test_charge_count_and_seed_setup_minutes_are_unknowns(self):
        out = self.landed("card_priced_raw_crew_drafts")
        by = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("CHARGE_COUNT_UNDERIVED", "vessel charges at 30000 units"), by)
        self.assertIn(("CHARGE_COUNT_UNDERIVED", "vessel charges at 8000 units"), by)
        self.assertNotIn("SETUP_STANDARD_DEFAULTED", {code for code, _ in by})
        sc = json.loads(json.dumps(scenario("card_priced_raw_crew_drafts")))
        sc["db"]["lineActionSetupStandard"] = []  # routing setup minutes filled from the seed
        facts = facts_of(sc)
        facts["vessel_plans"] = {"30000": {"charges": 2, "_source": F + "vessel_plan/30000"}}
        out = lc.quote_landed(facts, quote_currency="USD",
                              cost_currency={"currency": "USD", "_source": F + "fin"},
                              asked_volumes=sc["asked_volumes"])
        by = {(u["code"], u["subject"]) for u in out["unknowns"]}
        self.assertIn(("SETUP_STANDARD_DEFAULTED", "routing setup minutes"), by)
        self.assertIn(("SETUP_STANDARD_DEFAULTED", "changeover minutes at 30000 units"), by)
        self.assertNotIn(("CHARGE_COUNT_UNDERIVED", "vessel charges at 30000 units"), by)
        column = next(c for c in out["portal"]["volumeMatrix"] if c["askedUnits"] == 30000)
        self.assertEqual(column["labour"]["compoundingBasis"], "crew_per_charge")
        self.assertIn(F + "vessel_plan/30000", out["inputs_cited"])
        for bad in (0, 1.5, "2", None):
            facts["vessel_plans"] = {"30000": {"charges": bad, "_source": F + "v"}}
            with self.assertRaises(lc.LandedCostError):
                lc.quote_landed(facts, quote_currency="USD", cost_currency=None,
                                asked_volumes=sc["asked_volumes"])


class FreightQuoteTest(unittest.TestCase):
    """LOG-10 (source record 2e7013f1 F05; process_stage_map.json
    corrections_from_verified_originals): the two SoapBox forwarder quotes."""

    DESTINATION = {"_source": "original://freight-quote/fcl-baltimore", "amount": "1710",
                   "currency": "USD", "basis": "40HQ", "includes": ["destination_charges"],
                   "valid_until": "2026-09-30", "label": "FCL standard, Baltimore"}
    AIR = {"_source": "original://freight-quote/air-iad", "amount": "83965.56",
           "currency": "USD", "basis": "shipment",
           "includes": ["origin_charges", "main_carriage", "destination_charges"],
           "excludes": ["duties_and_taxes", "surcharges_contingencies"],
           "valid_until": "2026-09-30", "label": "Air to IAD all-in quotation.pdf"}

    def test_destination_charges_are_not_total_freight(self):
        out = lc.assess_freight_quote(self.DESTINATION, as_of="2026-09-29",
                                      basis_count={"count": 1, "_source": "original://rfq"})
        self.assertFalse(out["usable_as_total_freight"])
        self.assertIsNone(out["total_freight"])
        self.assertEqual([u["code"] for u in out["unknowns"]], ["FREIGHT_SCOPE_PARTIAL"])
        self.assertIn("origin_charges, main_carriage", out["unknowns"][0]["detail"])
        self.assertEqual(out["unit"], "USD/40HQ")

    def test_air_all_in_excludes_taxes_and_is_not_total_landed_freight(self):
        out = lc.assess_freight_quote(self.AIR, as_of="2026-09-29")
        codes = [u["code"] for u in out["unknowns"]]
        self.assertFalse(out["usable_as_total_freight"])
        self.assertIn("FREIGHT_LABEL_CONTRADICTS_SCOPE", codes)
        self.assertIn("FREIGHT_EXCLUDES_SURCHARGES", codes)
        self.assertTrue(out["duties_and_taxes_separate"])
        spec = lc.freight_cost_input(out)
        self.assertIn("unknown", spec)
        record = lc.compute_landed_cost(
            [{"name": "goods", "category": "goods", "literal": "40100", "unit": "USD"}, spec,
             {"name": "duty", "category": "duty", "unknown": "Section 301 needs the HS code",
              "who_can_answer": "Finance (customs broker)"}],
            {"literal": 50000, "unit": "EA"}, "USD", as_of="2026-09-29")
        self.assertEqual((record["status"], record["total"]), ("unknown", None))
        self.assertEqual({u["input"] for u in record["unknowns"]}, {"freight", "duty"})

    def test_expired_and_uncounted_quotes_are_unknown(self):
        quote = dict(self.DESTINATION, includes=["origin_charges", "main_carriage",
                                                 "destination_charges"])
        out = lc.assess_freight_quote(quote, as_of="2026-10-01")
        self.assertEqual([u["code"] for u in out["unknowns"]],
                         ["FREIGHT_QUOTE_EXPIRED", "FREIGHT_BASIS_COUNT_UNKNOWN"])

    def test_a_complete_valid_cited_quote_is_a_derived_freight_input(self):
        quote = dict(self.DESTINATION, amount="9800", includes=[
            "origin_charges", "main_carriage", "destination_charges"])
        out = lc.assess_freight_quote(quote, as_of="2026-09-29",
                                      basis_count={"count": 2, "_source": "original://pack-out"})
        self.assertTrue(out["usable_as_total_freight"])
        self.assertEqual(out["total_freight"], {"value": "19600", "unit": "USD",
                                                "basis_count_source": "original://pack-out"})
        spec = lc.freight_cost_input(out)
        record = lc.compute_landed_cost(
            [{"name": "goods", "category": "goods", "literal": "40100", "unit": "USD"}, spec,
             {"name": "duty", "category": "duty", "literal": "2606.50", "unit": "USD"}],
            {"literal": 50000, "unit": "EA"}, "USD", as_of="2026-09-29",
            resolver=lambda handle, actor: {"original": handle})
        self.assertEqual(record["status"], "computed")
        self.assertEqual(record["total"], {"value": "62306.5", "unit": "USD"})
        freight = next(i for i in record["inputs"] if i["name"] == "freight")
        self.assertEqual(freight["kind"], "derived")
        self.assertEqual(freight["derived_from"], ["original://freight-quote/fcl-baltimore",
                                                   "original://pack-out"])

    def test_scope_and_amount_are_validated(self):
        with self.assertRaises(lc.LandedCostError):
            lc.assess_freight_quote(dict(self.AIR, includes=["everything"]), as_of="2026-09-29")
        with self.assertRaises(lc.LandedCostError):
            lc.assess_freight_quote(dict(self.AIR, amount="0"), as_of="2026-09-29")
        with self.assertRaises(lc.LandedCostError):
            lc.assess_freight_quote({k: v for k, v in self.AIR.items() if k != "_source"},
                                    as_of="2026-09-29")


class PurchaseOrderLandedTest(unittest.TestCase):
    PO = {"_source": "api://public.app_purchase_order/po-1", "shippingCost": "1710",
          "dutiesCost": None}
    LINES = [{"id": "a", "_source": "api://l/a", "unitPrice": "0.802", "quantity": "12500",
              "quantityReceived": "12500"},
             {"id": "b", "_source": "api://l/b", "unitPrice": "0.802", "quantity": "12500",
              "quantityReceived": "12500"},
             {"id": "c", "_source": "api://l/c", "unitPrice": "1.13", "quantity": "3000",
              "quantityReceived": "3000"}]

    def test_freight_follows_the_portal_allocation(self):
        out = lc.po_line_landed(self.PO, self.LINES, "a")
        [golden] = [c for c in GOLDEN["functions"]["allocatePoFreightPerUnit"]
                    if c["args"]["shippingCost"] == 1710]
        self.assertEqual(out["portal_allocation"], golden["result"])
        self.assertEqual(out["freight"]["derived"]["value"],
                         lc.js_string(golden["result"]["freightPerUnit"]))
        self.assertTrue(out["freight"]["derived"]["per_quantity_unit"])
        self.assertIn("api://public.app_purchase_order/po-1", out["freight"]["derived"]["from"])
        self.assertTrue(out["duty"]["unknown"].startswith("TARIFF_TREATMENT_UNVERIFIED"))
        self.assertTrue(out["receipt_actual"]["unknown"].startswith("RECEIPT_ACTUAL_NOT_READ"))

    def test_unreceived_siblings_leave_freight_unknown_not_over_allocated(self):
        """Review M1: with only line a received the Portal puts the whole 1710 on a
        (0.1368 USD/EA instead of 0.0585): kept for comparison, never derived."""
        lines = [self.LINES[0]] + [dict(x, quantityReceived="0") for x in self.LINES[1:]]
        out = lc.po_line_landed(self.PO, lines, "a")
        self.assertEqual(out["portal_allocation"], {"freightPerUnit": 0.1368, "reason": None})
        self.assertTrue(out["freight"]["unknown"].startswith("PO_SIBLING_LINES_NOT_RECEIVED"))
        self.assertEqual(lc.po_line_landed(self.PO, self.LINES, "a")["portal_allocation"],
                         {"freightPerUnit": 0.0585, "reason": None})

    def test_zero_or_absent_shipping_is_unknown_not_free(self):
        for shipping in (None, "0", 0):
            out = lc.po_line_landed(dict(self.PO, shippingCost=shipping), self.LINES, "a")
            self.assertEqual(out["portal_allocation"]["freightPerUnit"], 0.0)  # Portal's reading
            self.assertTrue(out["freight"]["unknown"].startswith("PO_FREIGHT_NOT_RECORDED"))

    def test_unreceived_or_partly_received_lines_leave_freight_unknown(self):
        lines = [dict(x, quantityReceived=None) for x in self.LINES]
        out = lc.po_line_landed(self.PO, lines, "a")
        self.assertTrue(out["freight"]["unknown"].startswith("PORTAL_FREIGHT_UNALLOCATED"))
        self.assertIn("po-line-nothing-received", out["freight"]["unknown"])
        lines = [dict(self.LINES[0], quantityReceived="6000")] + self.LINES[1:]
        out = lc.po_line_landed(self.PO, lines, "a")
        self.assertTrue(out["freight"]["unknown"].startswith("PO_LINE_PARTLY_RECEIVED"))

    def test_duty_needs_a_cited_classification_and_has_no_per_line_allocation(self):
        """Review M2: a header duty estimate alone is not a tariff treatment."""
        po = dict(self.PO, dutiesCost="2606.5")
        out = lc.po_line_landed(po, self.LINES[:1], "a")
        self.assertTrue(out["duty"]["unknown"].startswith("TARIFF_TREATMENT_UNVERIFIED"))
        self.assertIn("2606.5", out["duty"]["unknown"])
        tariff = {"hs_code": "3923.30", "duty_rate": "0.065", "_source": "broker://hs/3923.30"}
        out = lc.po_line_landed(po, self.LINES, "a", tariff=tariff)
        self.assertTrue(out["duty"]["unknown"].startswith("DUTY_ALLOCATION_RULE_ABSENT"))
        out = lc.po_line_landed(po, self.LINES[:1], "a", tariff=tariff)
        self.assertEqual(out["duty"]["derived"]["value"], "2606.5")
        self.assertEqual(out["duty"]["derived"]["from"],
                         ["api://public.app_purchase_order/po-1", "broker://hs/3923.30"])
        self.assertFalse(out["duty"]["derived"]["per_quantity_unit"])
        self.assertIn("unknown", out["receipt_actual"])  # receipt actual still unread

    def test_derived_sources_must_resolve(self):
        spec = {"name": "freight", "category": "freight", "unit": "USD",
                "derived": {"value": "5", "from": ["api://x"], "rule": "r"}}
        goods = {"name": "goods", "category": "goods", "literal": "10", "unit": "USD"}
        record = lc.compute_landed_cost([goods, spec], {"literal": 1, "unit": "EA"}, "USD",
                                        as_of="2026-09-29")
        self.assertEqual(record["unknowns"][0]["reason"], "derived_source_unresolvable")
        record = lc.compute_landed_cost([goods, spec], {"literal": 1, "unit": "EA"}, "USD",
                                        as_of="2026-09-29",
                                        resolver=lambda h, a: {"id": h})
        self.assertEqual(record["total"], {"value": "15", "unit": "USD"})
        self.assertIn("api://x", record["inputs"][1]["derived_from_digests"])

    def test_derived_inputs_need_sources_and_a_rule(self):
        base = {"name": "freight", "category": "freight", "unit": "USD"}
        for bad in ({"value": "1"}, {"value": "1", "from": ["x"]}, {"value": 1, "from": ["x"],
                                                                      "rule": "r"}):
            with self.assertRaises(lc.LandedCostError):
                lc.compute_landed_cost([{**base, "derived": bad}], {"literal": 1, "unit": "EA"},
                                       "USD", as_of="2026-09-29")


# ---- pallet builder and skid math (LOG-01, LOG-08) -------------------------------------------
#
# Golden: the Workbench port of the Portal's pallet builder and skid math. Evidence label:
# ``isolated-fixture``.
# The expected pallet values are the outputs of the Portal's own TypeScript
# (petralabx/plx-customer-portal staging @48d76e8: ``portal/src/lib/wms/pallet-build.ts``,
# ``pallet-layout.ts``, ``portal/src/lib/3pl/skid-math.ts``) run stand-alone by
# ``operating/tests/portal_engine/pallet.mjs`` over the SYNTHETIC cases in
# ``fixtures/pallet_build_scenarios.json``; captured in ``fixtures/pallet_build_golden.json``.
# Every value must match.

PALLET_SCENARIOS = json.loads((FIXTURES / "pallet_build_scenarios.json").read_text("utf-8"))
PALLET_GOLDEN = json.loads((FIXTURES / "pallet_build_golden.json").read_text("utf-8"))


def _pallet_spec(args):
    if not args.get("spec"):
        return None
    return {**PALLET_SCENARIOS["specs"][args["spec"]], **args.get("patch", {})}


PALLET_CALLS = {
    "casesPerLayer": lambda a: pb.cases_per_layer(*a),
    "dimWeight": lambda a: pb.dim_weight(*a),
    "densityPcf": lambda a: pb.density_pcf(*a),
    "freightClassFromDensity": lambda a: [pb.freight_class_from_density(x) for x in a[0]],
    "buildPallets": lambda a: pb.build_pallets(
        _pallet_spec(a), quantity_cases=a.get("quantityCases"),
        quantity_units=a.get("quantityUnits"),
        mode=a.get("mode", "ltl"), description=a.get("description"),
        overhang_pct=a.get("overhangPct"), max_layers=a.get("maxLayers"),
        pattern=a.get("pattern")),
    "computeSkids": lambda a: pb.compute_skids(a["cases"], _pallet_spec(a),
                                               a.get("explicitSkids")),
    "caseVolumeCuFt": lambda a: pb.case_volume_cu_ft(_pallet_spec(a)),
    "packSpecDimsComplete": lambda a: pb.pack_spec_dims_complete(_pallet_spec(a)),
}



class GoldenPalletTest(unittest.TestCase):
    def test_every_case_matches_the_portal_typescript(self):
        self.assertEqual(PALLET_GOLDEN["portal_commit"], pb.PORTAL_REVISION)
        self.assertEqual({r["fn"] for r in PALLET_GOLDEN["results"]}, set(PALLET_CALLS))
        for case in PALLET_GOLDEN["results"]:
            with self.subTest(fn=case["fn"], args=case["args"]):
                self.assertEqual(PALLET_CALLS[case["fn"]](case["args"]), case["result"])

    def test_golden_is_current(self):
        """Re-run the harness on the checkout; skipped without Node >= 22.6, the checkout or
        when a pinned file moved (a moved Portal is re-pinned deliberately)."""
        if not (PORTAL / "portal/src").is_dir():
            self.skipTest("Portal checkout not present")
        version = _node_version()
        if version is None or version < (22, 6):
            self.skipTest("Node >= 22.6 (--experimental-transform-types) not present")
        for rel, digest in PALLET_GOLDEN["portal_src_files"].items():
            path = PORTAL / rel
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                self.skipTest(f"Portal checkout moved past the pinned {rel}; re-pin the golden")
        env = {"PATH": os.environ.get("PATH", ""), "TZ": "UTC",
               "PORTAL_SRC": str(PORTAL / "portal/src"),
               "PORTAL_COMMIT": PALLET_GOLDEN["portal_commit"]}
        out = subprocess.run(
            ["node", "--experimental-transform-types", "--no-warnings", "--import",
             "./register.mjs", "pallet.mjs", str(FIXTURES / "pallet_build_scenarios.json")],
            cwd=HARNESS, env=env, capture_output=True, text=True, timeout=120, check=True)
        self.assertEqual(json.loads(out.stdout), PALLET_GOLDEN)


if __name__ == "__main__":
    unittest.main()
