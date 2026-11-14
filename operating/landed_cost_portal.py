"""The Portal's landed-cost / quote engine, ported line by line (the frozen compatibility
port).

It computes in doubles exactly as the Portal's TypeScript does and is golden-tested bit for bit
against it (``operating/tests/test_portal_landed_engine.py``, harnesses in
``operating/tests/portal_engine``). Change it only to follow a Portal change, re-pinned and
re-goldened. The Workbench contract around it (Decimal money, units, currencies, citations,
typed unknowns, plan validation and presentation) is ``operating.landed_cost``, which
re-exports every name here that its callers use.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from ontology.units import js_trim
from operating.landed_cost_base import COST_TREATMENT, LandedCostError

# --------------------------------------------------------------------------- Portal engine port
#
# The Portal's landed-cost / quote engine (ERP-D-015 context), ported line by line from
# petralabx/plx-customer-portal staging @ 45673ee and re-pinned at d4d1bb2 (see below). Since the bc6e8de reading (TASK-2486) only
# 77f1ee6 (TASK-2465, G9) moved the cited files: project-landed-quote.ts, raw-cost.ts,
# formulation-resolution.ts and ready-to-quote.ts price what the customer supplies at $0
# through one G3 supply-source read (lib/customer-materials/supply-source*.ts, ported as
# ``resolve_supply_sources``); a failed read stops the quote, never PLX pricing. Since 514e1e5
# (TASK-2611, G-D4) the matrix unit-cost fallback skips a column with customer-supplied
# material (``fallback_matrix_unit_cost``); 65a0e8b only added an audit constant to
# supply-source-shared.ts. Re-read at 4243499 (2026-10-14): 4dc5cdf (TASK-2809) moved the
# engine's reads into quote-inputs.ts ``loadQuoteInputs`` (one setup-standards read,
# ``loadSetupStandardRows``; a failed read still resolves to the workbook seed and is now
# also named in ``readErrors``) and its pricing, unchanged, into ``priceQuoteInputs``;
# 4f8c905 (TASK-2810) added a dry-run cost preview, a POST the Workbench never calls (GET
# only); 66cd56c (TASK-1509) added ``pickRunVolumeTierForQtyExplicit`` for the L9 run-size
# view and left ``pickRunVolumeTierForQty`` as is. Overhead stays RateSettings $/hour
# (``overheadBasis.source = "rate_settings"``): ERP-D-022's line-hour rate is Proposed, not
# code. Both harnesses (operating/tests/portal_engine) re-run on 4243499 print every golden
# value unchanged (only the read list of one scenario lost a raw query: b59b007, TASK-2649,
# projects/read.ts ``loadCustomerLite`` now asks customers/portal-label.ts ``loadCustomerLabel``,
# portal customer records first, a display label that prices nothing), so nothing ported
# moved; the line citations below stay at 45673ee. Re-read at 1f2d42d (2026-10-14), only
# writes changed: 9108064 (TASK-2850, CT-B) makes quote-inputs.ts ``loadPrimaryRoutingSafe``
# always read with ``persist: false`` and the recalc script skip frozen FGs on --apply;
# 255ec2a (TASK-2863, RV2a) makes fg-routing-store.ts fill blank setup minutes in memory on
# every read (``withSetupFilled``, the old ``persist: false`` branch, which ``primary_routing``
# already ports); b236bef (TASK-2859, CT-H) stops draft-cost-persist.ts ``persistRow`` writing
# ``overheadPerUnit`` and non-compounding ``yieldUnits``, while ``loadDraftCostStore`` still
# reads the stored values and the engine still prices from an assembly row's stored
# ``yieldUnits`` (``resolveExpectedUnitsOverride``, project-landed-quote.ts@1f2d42d:401,2092;
# here ``_DRAFT_FIELDS``, ``resolve_expected_units_override``). Such a value can predate
# b236bef and no longer changes from the cost sheet. No ported arithmetic changed.
# Re-read at 40237ed (2026-10-15): 0ae127a (TASK-2851, RV2b) adds draft-cost-persist.ts
# ``loadCostSheetFreeze`` and makes ``assertCostSheetWritable`` refuse a frozen FG
# (``findFrozenFgItem``) with 409 ``FG_FROZEN`` before its other checks, so a frozen FG's tier
# rows no longer change from the cost sheet or the scratch cost write; the reads
# (``toDraftRow``, ``loadDraftCostStore``) are unchanged, and both harnesses print the same
# output with that file at 1f2d42d or at 40237ed.
# Re-read at d4d1bb2 (2026-10-15, drift-1015-portal-costing); the citations below and both
# goldens now pin d4d1bb2. 35cfb18 (TASK-2811) changed the one ported behaviour: the census
# labour detail of an unrouted quote now says it is priced at $0 and reported, not "the 1-hour
# placeholder" (``derive_ready_to_quote``); the arithmetic was already $0 in both. 0f9fbee
# (TASK-2948) has quote-inputs.ts read the setup standards once and hand the rows to the routing
# fill (``getPrimaryRouting(..., {persist: false, setupStandardRows})``): the same in-memory
# fill, a failed read still the workbook seed, as ``primary_routing`` does. eddbfe3 (TASK-2914)
# stops a routing *save* filling blank setup minutes and lets a manual save keep its own
# throughput (fg-routing.ts ``deriveFgRouting``), and 0a62186 (TASK-2864, RV2a) makes a primary
# routing save also write a one-band route plan and refuse a frozen FG with 409 ``FG_FROZEN``:
# writes the Workbench never makes; no read changed. 371b4cc (TASK-2865) appends rate-settings
# audit redaction to rate-masters.ts (``RATE_MASTER_DEFAULTS`` and ``overheadRateFromSettings``
# unchanged); the Workbench serves no app_audit_log. d4d1bb2 (TASK-2862, CT-C) adds a
# report-only cost fingerprint to the snapshot row that ``landedQuoteOf`` strips. ERP-D-022:
# b8f6e22 (TASK-2853) adds the line-hour overhead store ``LineOverheadRate`` and the flag
# ``User.canManageOverheadRates``, and 371b4cc its store and sizing code, but the engine does
# not read the store yet (overhead-rate-store.ts: "Not read by the quote engine yet (CT-G)";
# quote-inputs.ts ``overheadBasis.source`` is still "rate_settings"), so neither is ported.
# Both harnesses print every other golden value unchanged at d4d1bb2.
# ``readErrors`` is not a separate output here: a relation whose read failed arrives as None,
# is priced with the Portal fallback and is named as a RECORDS_NOT_READ unknown
# (``QUOTE_RELATIONS``), so the quote is never shown as verified while one is open.
# Re-read at d53137e (2026-10-16, drift-1015-portal-followup): no file cited here changed.
# 335dc8a (TASK-2878, CT-E2) has recalc-theoretical-fg.ts ``--apply`` also re-stamp a draft
# whose costs did not move but whose fingerprint is missing or stale, and 8c68104 (TASK-2918)
# adds the report-only TheoreticalFG freshness columns that Recalc and Activate write: writes
# the Workbench never makes, from the fingerprint ``landedQuoteOf`` strips.
#
# Arithmetic is IEEE double precision exactly as the Portal computes it (Python floats are
# the same doubles; Math.round is reproduced with ties toward +infinity), because the goal is to
# reproduce Portal numbers bit for bit. Every number leaves through ``portal_decimal`` (the
# shortest round-trip decimal, the digits JavaScript prints) with its unit and currency stated.
# The Portal substitutes 0 or a default for missing inputs (missing hours, no supplier card,
# no fill weight, no freight); the Workbench layer (``quote_landed``) lists each substitution as
# a typed unknown and states no Workbench cost while one is open: the Portal figure is shown as
# "what the Portal engine computes from these facts", never as a verified landed cost.
#
# The engine reads records, not resolved values: the formula pin and FG pin resolution, the
# ASSEMBLY BOM selection, the primary routing (setup minutes filled from the setup standards),
# the draft tier rows and the labour hours (LOG-08) are ported. Inputs, not derived: each asked
# volume's vessel charge count (the compounding standard and vessel derivation), the quote run
# quantity, and the census / pack spec, which have no read-only export upstream (the matrix and
# workbook GETs write a theoretical-FG snapshot, so they are never read sources). The Q2-C volume
# advisories are not ported. The WMS pallet builder and skid math are ported in
# operating/pallet_build.py (domestic parcel/LTL handling units and freight class; they price no
# ocean or landed freight).

PORTAL_REVISION = "d4d1bb247708052ddacf78ed4f47bcba0a27c375"
PORTAL_SHORT = "d4d1bb2"
_L = "portal/src/lib/"


def _psrc(path: str, lines: str) -> str:
    return f"{path}@{PORTAL_SHORT}#L{lines}"


_PLQ = _L + "costing/project-landed-quote.ts"
_QIN = _L + "costing/quote-inputs.ts"
PORTAL_SOURCES = {
    "landed_component": _psrc(_L + "costing/landed-cost.ts", "39-71"),
    "fill_mass": _psrc(_L + "costing/landed-cost.ts", "73-81"),
    "rate_defaults": _psrc(_L + "costing/rate-masters.ts", "34-57"),
    "overhead_rate": _psrc(_L + "costing/rate-masters.ts", "71-80"),
    "suggestion_volumes": _psrc(_L + "costing/rate-masters.ts", "133-156"),
    "estimate_unit_cost": _psrc(_L + "costing/cost-estimate.ts", "111-198"),
    "margin_ladder": _psrc(_L + "costing/margin-ladder.ts", "24-182"),
    "uptime_overhead": _psrc(_L + "costing/uptime-overhead.ts", "50-121"),
    "workbook_wedge": _psrc(_L + "costing/setup-standards.ts", "19-23"),
    "run_volume_tier": _psrc(_L + "costing/volume-tiers.ts", "77-86")
    + "; " + _psrc(_L + "costing/template-cost-matrix.ts", "44-49"),
    "parse_money": _psrc(_PLQ, "339-343"),
    "margin_and_overhead": _psrc(_PLQ, "361-424"),
    "gather_quote_facts": _psrc(_PLQ, "651-888"),
    "census_from_facts": _psrc(_PLQ, "899-927"),
    "card_tier": _psrc(_PLQ, "1304-1328"),
    "po_freight_allocation": _psrc(_PLQ, "1340-1366"),
    "line_cost_evidence": _psrc(_PLQ, "1392-1521"),
    "line_provenance": _psrc(_PLQ, "1529-1602"),
    "volume_column": _psrc(_PLQ, "1713-1987") + "; " + _psrc(_PLQ, "1030-1132"),
    "matrix_landed_caller": _psrc(_PLQ, "1881-1884"),
    "engine": _psrc(_PLQ, "2092-2327") + "; " + _psrc(_QIN, "111-164")
    + "; " + _psrc(_PLQ, "2371-2404"),
    "raw_cost": _psrc(_L + "costing/raw-cost.ts", "1-59"),
    "formulation_resolution": _psrc(_L + "costing/formulation-resolution.ts", "1-181")
    + "; " + _psrc(_L + "project-development/overview-shared.ts", "61-92"),
    "bom_selection": _psrc(_PLQ, "527-649"),
    "routing_labour": _psrc(_L + "costing/fg-routing.ts", "89-192"),
    "template_labour": _psrc(_L + "costing/template-cost-matrix.ts", "84-101"),
    "setup_amortisation": _psrc(_L + "costing/setup-standards.ts", "208-271"),
    "setup_standard": _psrc(_L + "costing/setup-standards.ts", "19-169")
    + "; " + _psrc(_L + "costing/setup-standards-store.ts", "98-119")
    + "; " + _psrc(_L + "inventory/line-tank-keys.ts", "34-45"),
    "routing_setup_fill": _psrc(_L + "costing/fg-routing-store.ts", "73-298")
    + "; " + _psrc(_QIN, "90-104"),
    "draft_costs": _psrc(_L + "costing/draft-cost-overrides.ts", "92-117")
    + "; " + _psrc(_L + "costing/draft-cost-overrides.ts", "420-443")
    + "; " + _psrc(_L + "assembly-bom/draft-cost-persist.ts", "113-175")
    + "; " + _psrc(_L + "assembly-bom/draft-cost-persist.ts", "353-374")
    + "; " + _psrc(_L + "costing/volume-tiers.ts", "26-69"),
    "quote_hours": _psrc(_PLQ, "951-1259") + "; " + _psrc(_PLQ, "408-424"),
    "setup_standard_load": _psrc(_QIN, "111-164")
    + "; " + _psrc(_L + "costing/setup-standards-store.ts", "98-111"),
    "bom_total": _psrc(_L + "costing/bom-total.ts", "14-26"),
    "ready_to_quote": _psrc(_L + "costing/ready-to-quote.ts", "64-497"),
    "quote_totals": _psrc(_L + "quote/totals.ts", "8-57"),
    "matrix_fallback": _psrc(_L + "quote/matrix-fallback.ts", "15-95"),
    "matrix_offer_margin": _psrc(_L + "quote/matrix.ts", "149-183"),
    "matrix_route_fallback": _psrc("portal/src/app/api/mrp/quotes/[id]/matrix/route.ts",
                                   "134-162"),
    "offer_writer": _psrc(_L + "quote/service.ts", "389-480"),
    "landed_gl": _psrc(_L + "procurement/landed-gl.ts", "1-104"),
    "customer_supplied": _psrc(_PLQ, "184-267"),
    "supply_source": _psrc(_L + "customer-materials/supply-source-shared.ts", "14-82")
    + "; " + _psrc(_L + "customer-materials/supply-source.ts", "46-233")
    + "; " + _psrc(_L + "customers/anchor.ts", "42-43")
    + "; " + _psrc(_L + "customers/anchor.ts", "99-118")
    + "; " + _psrc(_L + "sourcing/raw-product.ts", "20-48")
    + "; " + _psrc(_L + "sourcing/status.ts", "74-99")
    + "; " + _psrc(_L + "projects/read.ts", "120-152"),
    "erp_d_015": "docs/projects/business-central-erp-modernization/decision-log.md@"
                 + PORTAL_SHORT + "#L25",
}

# portal/src/lib/costing/rate-masters.ts RATE_MASTER_DEFAULTS
RATE_DEFAULTS = {"rawMaterialsAdjustmentPct": 10.0, "batchingRate": 32.0, "assemblyRate": 20.9,
                 "overheadRate": 31.3, "marginPct": 30, "marginTiersPct": (25, 30, 35, 40),
                 "quoteSuggestionVolumes": (5000, 10000, 25000, 50000), "fxRateCadUsd": 1.0,
                 "tariffRate": 0.065, "landedMultiplier": 1.38}
PORTAL_QUOTE_DUTY_RATE = RATE_DEFAULTS["tariffRate"]
PORTAL_QUOTE_MULTIPLIER = RATE_DEFAULTS["landedMultiplier"]
PORTAL_QUOTE_SOURCE = (PORTAL_SOURCES["landed_component"] + " (landedComponentCost); callers "
                       "project-landed-quote.ts:784 and :1700, quote/matrix.ts:111, "
                       "quote/workbook.ts:208, costing/quote-advisories.ts:407 pass only "
                       "tierPrice and freight")
WORKBOOK_UPTIME_HOURS = 6.5  # setup-standards.ts WORKBOOK_WEDGE_HOURS.uptime
# template-cost-matrix.ts RUN_VOLUME_TIERS (code, maxQty), sorted by sortOrder
RUN_VOLUME_TIERS = (("5k", 9999), ("10k", 24999), ("25k", 49999), ("50k", None))
DRIFT_THRESHOLD_PCT = 10  # ready-to-quote.ts DEFAULT_CARD_PO_DRIFT_THRESHOLD_PCT
STOCK_FILL_LABEL = ("Margin if filled from stock — won't-replace-stock exception, display only; "
                    "the priced margin stays canonical")
LANDED_GL = {"rmFreight": "41102", "packFreight": "41130", "rmDuty": "41101",
             "packDuty": "41131", "distDuty": "41680"}
_EPSILON = 2.220446049250313e-16  # Number.EPSILON
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---- JavaScript number semantics -------------------------------------------------------------

def _is_num(value: Any) -> bool:
    """``typeof value === "number"`` (NaN and infinities included, bools excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    """``Number.isFinite(value)``."""
    return _is_num(value) and math.isfinite(value)


def _num(value: Any) -> float:
    """``num()`` of landed-cost.ts / cost-estimate.ts: a finite number above zero, else 0."""
    return float(value) if _finite(value) and value > 0 else 0.0


def _truthy(value: Any) -> bool:
    """JavaScript truthiness for the values a Prisma row holds."""
    if value is None or value is False:
        return False
    if _is_num(value):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def _nc(*values: Any) -> Any:
    """``a ?? b ?? c``: the first value that is not null/undefined."""
    for value in values:
        if value is not None:
            return value
    return None


def _js_math_round(value: float) -> float:
    """``Math.round``: nearest integer, ties toward +infinity, exactly as in IEEE doubles."""
    if not math.isfinite(value):
        return value
    floor = math.floor(value)
    return float(floor + 1 if value - floor >= 0.5 else floor)


def _round(value: float, scale: float) -> float:
    """``Math.round(n * scale) / scale`` (round2 = 100, round4 = 1e4, round6 = 1e6)."""
    return _js_math_round(value * scale) / scale


def quote_round2(value: float) -> float:
    """quote/totals.ts ``round2``: ``Math.round((n + Number.EPSILON) * 100) / 100``."""
    return _js_math_round((value + _EPSILON) * 100) / 100


def js_number(value: Any) -> float | None:
    """``Number(value)`` for the row values the engine meets; None stands for NaN."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if _is_num(value):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        # StringToNumber: trimmed; "" is 0; decimal literal, "Infinity" with a sign, or an
        # unsigned 0x/0o/0b integer. Anything else ("1_000", "inf", "nan") is NaN.
        text = value.strip()
        if text == "":
            return 0.0
        if text in ("Infinity", "+Infinity", "-Infinity"):
            return -math.inf if text[0] == "-" else math.inf
        radix = {"0x": 16, "0o": 8, "0b": 2}.get(text[:2].lower())
        if radix:
            try:
                return float(int(text[2:], radix)) if "_" not in text and text[2:] else None
            except ValueError:
                return None
        if not _JS_DECIMAL.match(text):
            return None
        return float(text)
    return None


_JS_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _to_num(value: Any) -> float | None:
    """project-landed-quote.ts ``toNum``: ``Number(value)`` when finite, else null."""
    number = js_number(value)
    return number if number is not None and math.isfinite(number) else None


def js_string(value: Any) -> str:
    """JavaScript ``String(number)`` (Number::toString), for Portal text such as census details."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, exponent = Decimal(repr(abs(value))).normalize().as_tuple()[1:]
    s = "".join(map(str, digits))
    k = len(s)
    n = k + exponent  # decimal point position, as in ECMA-262 Number::toString
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        e = n - 1
        mantissa = s[0] + ("." + s[1:] if k > 1 else "")
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + body


def portal_decimal(value: Any) -> Decimal | None:
    """The exact decimal a Portal (JavaScript) number prints as; None for null/NaN/infinite."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float) and math.isfinite(value):
        return Decimal(repr(value))
    return None


def _iso(value: Any) -> str | None:
    """``toIso``: ``new Date(value).toISOString()`` (a time without an offset is read as UTC,
    the harness runs with TZ=UTC); None when the value is not a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _epoch_ms(iso: str | None) -> int | None:
    """``Date.parse`` of an ISO string, in whole milliseconds."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


# ---- pure Portal functions, one per Portal function ------------------------------------------

def landed_component_cost(tier_price: Any, freight: Any = 0, *, duty: Any = None,
                          duty_rate: Any = None, multiplier: Any = None) -> dict[str, Any]:
    """landed-cost.ts ``landedComponentCost``:
    ``duty = round4(tier × rate)``; ``landed = round4((tier + duty) × multiplier + freight)``.
    Every Portal caller passes only ``tierPrice`` and ``freight``, so rate 0.065 and
    multiplier 1.38 always apply (RateSettings tariffRate/landedMultiplier have no effect)."""
    tier = _num(tier_price)
    rate = float(duty_rate) if _finite(duty_rate) and duty_rate >= 0 else PORTAL_QUOTE_DUTY_RATE
    duty_value = float(duty) if _finite(duty) and duty >= 0 else _round(tier * rate, 1e4)
    mult = float(multiplier) if _finite(multiplier) and multiplier > 0 else PORTAL_QUOTE_MULTIPLIER
    freight_value = _num(freight)
    landed = _round((tier + duty_value) * mult + freight_value, 1e4)
    return {"tierPrice": _round(tier, 1e4), "duty": _round(duty_value, 1e4), "dutyRate": rate,
            "multiplier": mult, "freight": _round(freight_value, 1e4), "landedUnitCost": landed}


def portal_quote_landed_component(tier_price: Any, freight: Any = 0) -> dict[str, Any]:
    """The quote engine's per-component call ``landedComponentCost({tierPrice, freight})``,
    labelled: a Portal quote estimate, never item cost (ERP-D-015). No FX conversion: the tier
    price and freight are used in whatever currency the supplier card holds."""
    return {**landed_component_cost(tier_price, freight), "currency_conversion": None,
            "rate_settings_applied": False,
            "label": "Portal quote landed-cost estimate (mirrored), not item cost",
            "cost_treatment": dict(COST_TREATMENT), "source": PORTAL_QUOTE_SOURCE}


def fill_mass_kg(fill_weight_g: Any, specific_gravity: Any = None) -> float:
    """landed-cost.ts ``fillMassKg``: (g / 1000) × SG, SG defaulting to 1."""
    grams = _num(fill_weight_g)
    sg = float(specific_gravity) if _finite(specific_gravity) and specific_gravity > 0 else 1.0
    return (grams / 1000) * sg


def estimate_unit_cost(inp: Mapping[str, Any]) -> dict[str, Any]:
    """cost-estimate.ts ``estimateUnitCost`` (same operation order, same rounding)."""
    fill_kg = _num(inp.get("fillWeightG")) / 1000
    adj = _num(inp.get("rawMaterialsAdjustmentPct"))
    raw_per_kg = 0.0
    for row in inp.get("rawMaterials") or []:
        raw_per_kg = raw_per_kg + (_num(row.get("inclusionPct")) / 100) * _num(row.get("costPerKg"))
    bulk_per_kg = raw_per_kg * (1 + adj / 100)
    bulk = bulk_per_kg * fill_kg
    packaging = 0.0
    for row in inp.get("packaging") or []:
        packaging = packaging + _num(row.get("qtyPerUnit")) * _num(row.get("unitCost"))
    asm_hours = _num(inp.get("assemblyHoursPerUnit"))
    cmp_hours = _num(inp.get("compoundingHoursPerUnit"))
    production_hours = asm_hours + cmp_hours
    asm_labour = asm_hours * _num(inp.get("assemblyRate"))
    cmp_labour = cmp_hours * _num(inp.get("batchingRate"))
    oh_hours = (_num(inp.get("overheadHoursPerUnit"))
                if inp.get("overheadHoursPerUnit") is not None else asm_hours)
    overhead = oh_hours * _num(inp.get("overheadRate"))
    total = bulk + packaging + cmp_labour + asm_labour + overhead
    margin = _num(inp.get("marginPct"))
    sell = total / (1 - margin / 100) if margin < 100 else total
    r2 = lambda v: _round(v, 100.0)  # noqa: E731
    lines = [{"key": "bulk", "label": "Raw materials (bulk)", "amountPerUnit": r2(bulk)},
             {"key": "packaging", "label": "Packaging components", "amountPerUnit": r2(packaging)},
             {"key": "compounding", "label": "Compounding labour", "amountPerUnit": r2(cmp_labour)},
             {"key": "assembly", "label": "Assembly labour", "amountPerUnit": r2(asm_labour)},
             {"key": "overhead", "label": "Overhead", "amountPerUnit": r2(overhead)}]
    return {"bulkCostPerKg": r2(bulk_per_kg), "bulkCostPerUnit": r2(bulk),
            "packagingCostPerUnit": r2(packaging), "compoundingLabourPerUnit": r2(cmp_labour),
            "assemblyLabourPerUnit": r2(asm_labour), "overheadPerUnit": r2(overhead),
            "productionHoursPerUnit": _round(production_hours, 1e6),
            "totalCostPerUnit": r2(total), "marginPct": margin, "sellPricePerUnit": r2(sell),
            "marginPerUnit": r2(sell - total), "lines": lines}


def pick_run_volume_tier(qty: Any) -> str | None:
    """volume-tiers.ts ``pickRunVolumeTierForQty``."""
    if qty is None or not _finite(qty) or qty <= 0:
        return None
    for code, max_qty in RUN_VOLUME_TIERS:
        if qty <= (max_qty if max_qty is not None else math.inf):
            return code
    return RUN_VOLUME_TIERS[-1][0]


def _valid_margin(value: Any) -> bool:
    return _finite(value) and 0 <= value < 100


def margin_to_markup_pct(margin_pct: Any) -> float:
    if not _valid_margin(margin_pct):
        return math.nan
    m = margin_pct / 100
    return (m / (1 - m)) * 100


def price_at_margin_pct(cost: Any, margin_pct: Any) -> float:
    """margin-ladder.ts ``priceAtMarginPct``: cost ÷ (1 − m)."""
    base = float(cost) if _finite(cost) and cost > 0 else 0.0
    if not _valid_margin(margin_pct):
        return base
    return base / (1 - margin_pct / 100)


def _margin_label(margin_pct: float) -> str:
    markup = margin_to_markup_pct(margin_pct)
    shown = js_string(margin_pct if float(margin_pct).is_integer() else _round(margin_pct, 10.0))
    return f"{shown}% margin = {js_string(_round(markup, 10.0))}% markup"


def normalize_margin_tiers(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    ordered = sorted(_round(n, 100.0) for n in value if _valid_margin(n))
    clean = [n for i, n in enumerate(ordered) if i == 0 or abs(n - ordered[i - 1]) > 0.01]
    return clean or None


def resolve_margin_ladder(settings: Mapping[str, Any] | None,
                          quote_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """margin-ladder.ts ``resolveMarginLadder`` (OD-13)."""
    settings = settings or {}
    override = quote_override or {}
    system = normalize_margin_tiers(settings.get("marginTiersPct"))
    over = normalize_margin_tiers(override.get("tiersPct"))
    tiers = over or system or list(RATE_DEFAULTS["marginTiersPct"])
    tiers_source = "quote_override" if over else ("rate_settings" if system else "fallback")
    default = settings.get("marginPercent")
    default = default if _valid_margin(default) else RATE_DEFAULTS["marginPct"]
    target_override = override.get("targetPct")
    target = target_override if _valid_margin(target_override) else default
    return {"tiersPct": tiers, "tiersSource": tiers_source, "defaultTierPct": default,
            "targetPct": target,
            "targetSource": "quote_override" if _valid_margin(target_override) else "system_default",
            "tiers": [{"marginPct": m, "markupPct": margin_to_markup_pct(m),
                       "label": _margin_label(m), "isDefault": abs(m - default) <= 0.01,
                       "isTarget": abs(m - target) <= 0.01} for m in tiers]}


def price_margin_tiers(cost: Any, ladder: Mapping[str, Any]) -> list[dict[str, Any]]:
    """margin-ladder.ts ``priceMarginTiers``: every tier off one frozen cost."""
    return [{**tier, "sellPricePerUnit": _round(price_at_margin_pct(cost, tier["marginPct"]), 100.0)}
            for tier in ladder["tiers"]]


def overhead_rate_from_settings(rates: Mapping[str, Any] | None) -> float:
    """rate-masters.ts ``overheadRateFromSettings``: the first ``overheadPer*`` number."""
    for key, value in (rates or {}).items():
        if isinstance(key, str) and key.startswith("overheadPer") and _finite(value):
            return value
    return RATE_DEFAULTS["overheadRate"]


def derive_uptime_overhead(hourly_rate: Any, assembly_hours_per_unit: Any,
                           assembly_hours_source: Any = None, expected_units_per_shift: Any = None,
                           uptime_hours: Any = None) -> dict[str, Any]:
    """uptime-overhead.ts ``deriveUptimeOverhead`` (OD-14: assembly-only absorption)."""
    pos = lambda v: v if _finite(v) and v > 0 else None  # noqa: E731
    rate = hourly_rate if _finite(hourly_rate) and hourly_rate >= 0 else 0
    reasons: list[str] = []
    base = {"hourlyRate": rate, "hoursSource": _nc(assembly_hours_source), "appliesTo": "assembly"}
    expected = pos(expected_units_per_shift)
    if expected is not None:
        uptime = pos(uptime_hours)
        if uptime is None:
            uptime = WORKBOOK_UPTIME_HOURS
            reasons.append("uptime-defaulted-workbook-wedge")
        hours = uptime / expected
        return {**base, "uptimeHours": uptime, "expectedUnitsPerShift": expected,
                "hoursPerUnit": hours, "perUnit": rate * hours,
                "source": "expected_units_override", "reasons": reasons}
    hours = pos(assembly_hours_per_unit)
    if hours is not None:
        return {**base, "uptimeHours": None, "expectedUnitsPerShift": None, "hoursPerUnit": hours,
                "perUnit": rate * hours, "source": "assembly_hours", "reasons": reasons}
    reasons.append("overhead-basis-missing:no-assembly-hours")
    return {**base, "uptimeHours": None, "expectedUnitsPerShift": None, "hoursPerUnit": None,
            "perUnit": None, "source": "missing", "reasons": reasons}


def quote_suggestion_volumes(rates: Mapping[str, Any] | None) -> list[float]:
    value = (rates or {}).get("quoteSuggestionVolumes")
    if isinstance(value, (list, tuple)):
        seen: list[float] = []
        for n in value:
            if _finite(n) and n > 0:
                rounded = _js_math_round(float(n))
                if rounded not in seen:
                    seen.append(rounded)
        if seen:
            return sorted(seen)
    return list(RATE_DEFAULTS["quoteSuggestionVolumes"])


def pick_supplier_card_tier_price(card: Mapping[str, Any], component_qty: Any,
                                  run_volume_tier_code: str | None) -> dict[str, Any]:
    """project-landed-quote.ts ``pickSupplierCardTierPrice``: highest minQty ≤ demand."""
    matched = None
    if component_qty is not None and component_qty > 0:
        for tier in card.get("tiers") or []:
            if tier["minQty"] <= component_qty and (matched is None
                                                    or tier["minQty"] > matched["minQty"]):
                matched = tier
    return {"source": "supplier_card_tier",
            "unitCost": matched["unitCost"] if matched else card.get("unitCost"),
            "tierMinQty": matched["minQty"] if matched else None,
            "runVolumeTierCode": run_volume_tier_code, "qtyBasis": component_qty,
            "asOf": _iso(_nc(matched.get("effectiveFrom"), matched.get("updatedAt")))
            if matched else _iso(card.get("updatedAt"))}


def allocate_po_freight_per_unit(shipping_cost: Any, po_lines: Sequence[Mapping[str, Any]],
                                 line_id: Any) -> dict[str, Any]:
    """project-landed-quote.ts ``allocatePoFreightPerUnit``: header shippingCost pro-rata by
    RECEIVED line value, per received unit. Portal reads an absent or zero shippingCost as a
    genuine zero; the Workbench layer (``po_line_landed``) does not."""
    target = next((line for line in po_lines if line.get("id") == line_id), None)
    if target is None:
        return {"freightPerUnit": None, "reason": "po-line-not-found"}
    if not (_is_num(target.get("quantityReceived")) and target["quantityReceived"] > 0):
        return {"freightPerUnit": None, "reason": "po-line-nothing-received"}
    shipping = float(shipping_cost) if _finite(shipping_cost) and shipping_cost > 0 else 0.0
    if shipping == 0:
        return {"freightPerUnit": 0.0, "reason": None}

    def value(line: Mapping[str, Any]) -> float:
        return max(0.0, js_number(line.get("unitPrice")) or 0.0) * \
            max(0.0, js_number(line.get("quantityReceived")) or 0.0)

    total = 0.0
    for line in po_lines:
        total = total + value(line)
    if not total > 0:
        return {"freightPerUnit": None, "reason": "po-freight-unallocatable-zero-line-value"}
    share = shipping * (value(target) / total)
    return {"freightPerUnit": _round(share / target["quantityReceived"], 1e4), "reason": None}


def rollup_bom_total(lines: Sequence[Mapping[str, Any]]) -> float:
    """bom-total.ts ``rollupBomTotal``."""
    total = 0.0
    for line in lines:
        qty = line.get("quantity") if _finite(line.get("quantity")) else 0
        est = line.get("estimatedUnitCost")
        unit = est if est is not None and _finite(est) else (
            line.get("cost") if _finite(line.get("cost")) else 0)
        total = total + qty * unit
    return _round(total, 1e4)


def parse_money(value: Any) -> float | None:
    """project-landed-quote.ts ``parseMoney``: digits and dots only, finite and above zero."""
    if not _truthy(value):
        return None
    number = js_number("".join(ch for ch in js_string(value) if ch.isdigit() or ch == "."))
    return number if number is not None and math.isfinite(number) and number > 0 else None


# ---- ready-to-quote census (ready-to-quote.ts) ------------------------------------------------

def _to_time(iso: Any) -> int | None:
    return _epoch_ms(iso) if isinstance(iso, str) and iso else None


def derive_cost_provenance_advisories(lines: Sequence[Mapping[str, Any]],
                                      drift_threshold_pct: Any = None) -> list[dict[str, Any]]:
    threshold = drift_threshold_pct if drift_threshold_pct is not None and \
        drift_threshold_pct > 0 else DRIFT_THRESHOLD_PCT
    out: list[dict[str, Any]] = []
    for line in lines:
        card, po = line.get("cardUnitCost"), line.get("lastPoUnitPrice")
        if card is not None and po is not None and po > 0 and _finite(card) and _finite(po):
            drift = _js_math_round(((card - po) / po) * 1000) / 10
            if abs(drift) > threshold:
                out.append({"kind": "card_po_drift", "severity": "warning",
                            "lineName": line["lineName"], "cardUnitCost": card,
                            "lastPoUnitPrice": po, "driftPct": drift,
                            "detail": f"{line['lineName']}: supplier card ${js_string(card)} vs "
                                      f"last-PO ${js_string(po)} — "
                                      f"{'+' if drift > 0 else ''}{js_string(drift)}% drift "
                                      f"(threshold {js_string(threshold)}%). Card stays "
                                      "canonical; re-verify the card."})
        card_at, received_at = _to_time(line.get("cardUpdatedAt")), \
            _to_time(line.get("lastPoReceivedAt"))
        if card_at is not None and received_at is not None and card_at < received_at:
            out.append({"kind": "stale_card", "severity": "advisory", "lineName": line["lineName"],
                        "cardUpdatedAt": line.get("cardUpdatedAt"),
                        "lastPoReceivedAt": line.get("lastPoReceivedAt"),
                        "detail": f"{line['lineName']}: supplier card last updated "
                                  f"{line.get('cardUpdatedAt')} — older than the newest PO "
                                  f"receipt {line.get('lastPoReceivedAt')}. Card stays "
                                  "canonical; confirm it still reflects the vendor's price."})
    return out


def derive_margin_if_filled_from_stock(sell: Any, priced_total: Any,
                                       lines: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if sell is None or not sell > 0 or priced_total is None or not _finite(priced_total):
        return None
    delta = 0.0
    from_stock = at_priced = 0
    for line in lines:
        stock_cost, on_hand, required = (line.get("stockUnitCost"),
                                         line.get("stockQuantityOnHand"), line.get("requiredQty"))
        if stock_cost is not None and on_hand is not None and required is not None and \
                required > 0 and on_hand >= required:
            from_stock += 1
            delta = delta + line["qtyPerUnit"] * (stock_cost - line["pricedUnitCost"])
        else:
            at_priced += 1
    if from_stock == 0:
        return None
    adjusted = _round(priced_total + delta, 100.0)
    margin = lambda cost: _js_math_round(((sell - cost) / sell) * 1000) / 10  # noqa: E731
    return {"label": STOCK_FILL_LABEL, "sellPricePerUnit": sell,
            "pricedTotalCostPerUnit": _round(priced_total, 100.0),
            "pricedMarginPct": margin(priced_total), "stockAdjustedTotalCostPerUnit": adjusted,
            "stockAdjustedMarginPct": margin(adjusted), "linesFromStock": from_stock,
            "linesAtPricedBasis": at_priced}


def _count_of(value: Any) -> int:
    """ready-to-quote.ts ``countOf``: a finite count above 0, floored, else 0."""
    return math.floor(value) if _finite(value) and value > 0 else 0


def derive_ready_to_quote(inp: Mapping[str, Any]) -> dict[str, Any]:
    """ready-to-quote.ts ``deriveReadyToQuote`` (Q2-C ``volumeAdvisories`` pass through)."""
    items: list[dict[str, Any]] = []
    fill, sg = inp.get("fillWeightG"), inp.get("specificGravity")
    item = lambda k, label, sev, detail: {"key": k, "label": label,  # noqa: E731
                                          "severity": sev, "detail": detail}
    items.append(item("fill_weight_g", "Fill weight (g)", "ok", f"{js_string(fill)} g on the "
                      "ASSEMBLY BOM") if fill is not None and fill > 0 else
                 item("fill_weight_g", "Fill weight (g)", "blocker",
                      "ASSEMBLY BOM has no typed fill_weight_g (no silent 500 g default)"))
    if sg is not None and sg > 0 and inp.get("specificGravityConfirmed"):
        items.append(item("specific_gravity", "Specific gravity", "ok", js_string(sg)))
    else:
        items.append(item("specific_gravity", "Specific gravity", "warning",
                          f"Using {js_string(sg)} — confirm on the formulation"
                          if sg is not None and sg > 0 else
                          "Defaulting to 1.00 until Formulation.specificGravity is set"))
    unsourced, linked = inp["unsourcedLineCount"], inp["linkedLineCount"]
    # G9: customer-supplied lines are counted apart, so they never block here.
    lines = _count_of(inp.get("customerSuppliedLineCount"))
    pending = min(_count_of(inp.get("customerPendingLineCount")), lines)
    raws = _count_of(inp.get("customerSuppliedRawCount"))
    if unsourced == 0 and (linked > 0 or lines > 0):
        items.append(item("sourcing", "Packaging sourced", "ok",
                          f"All {lines} line(s) supplied by the customer" if linked == 0 else
                          f"{linked} line(s) linked; estimates cleared; {lines} supplied by the "
                          "customer" if lines > 0 else
                          f"{linked} line(s) linked; estimates cleared"))
    else:
        items.append(item("sourcing", "Packaging sourced",
                          "blocker" if unsourced > 0 else "warning",
                          f"{unsourced} line(s) still on estimatedUnitCost / needsSourcing"
                          if unsourced > 0 else "No ASSEMBLY BOM lines to quote"))
    if lines + raws > 0:
        what = " and ".join(part for part in (f"{lines} packaging line(s)" if lines else None,
                                              f"{raws} raw material(s)" if raws else None) if part)
        supplied = f"{what} supplied by the customer, priced at $0"
        items.append(item("customer_supplied", "Customer-supplied", "warning",
                          f'{supplied}; {pending} line(s) still waiting for "Approve customer '
                          'component"') if pending > 0 else
                     item("customer_supplied", "Customer-supplied", "advisory", supplied))
    hs = inp["missingHsCodeCount"]
    items.append(item("hs_code", "HS code", "ok", "Import HS code present on linked products")
                 if hs == 0 else item("hs_code", "HS code", "warning",
                                      f"{hs} linked product(s) missing Product.hsCode"))
    inc, org = inp["missingIncotermsCount"], inp["missingOriginCount"]
    items.append(item("vendor_card", "Vendor-item card", "ok",
                      "Incoterms and country of origin present") if inc == 0 and org == 0 else
                 item("vendor_card", "Vendor-item card", "warning",
                      f"{inc} missing incoterms, {org} missing origin"))
    items.append(item("labour", "Labour / overhead", "warning",
                      "No routing hours, so assembly and setup labour are priced at $0 and "
                      "reported")
                 if inp["labourIsPlaceholder"] else
                 item("labour", "Labour / overhead", "ok", "Routing hours pinned"))
    sell, retail = inp.get("sellPricePerUnit"), inp.get("retailPerUnit")
    pct = None
    if sell is not None and retail is not None and retail != 0 and _finite(sell) and \
            _finite(retail):
        pct = _js_math_round(((sell - retail) / retail) * 1000) / 10
    return {"ready": all(x["severity"] != "blocker" for x in items), "items": items,
            "sellVsRetail": {"sellPricePerUnit": sell, "retailPerUnit": retail, "deltaPct": pct,
                             "note": "No retail / target price on the project to compare"
                             if pct is None else
                             f"Suggested sell is {'+' if pct > 0 else ''}{js_string(pct)}% vs "
                             "retail / target"},
            "advisories": derive_cost_provenance_advisories(inp.get("provenanceLines") or [],
                                                            inp.get("driftThresholdPct")),
            "marginIfFilledFromStock": derive_margin_if_filled_from_stock(
                sell, inp.get("pricedTotalCostPerUnit"), inp.get("stockFillLines") or []),
            "volumeAdvisories": list(inp.get("volumeAdvisories") or [])}


# ---- quote totals, TASK-2175 matrix fallback, saved offer (lib/quote) -------------------------

def clamp_margin_percent(margin: Any) -> float:
    if not _finite(margin):
        return 0.0
    return min(max(float(margin), 0.0), 99.9)


def derive_unit_price(std_cost: float, margin: Any) -> float:
    return quote_round2(std_cost / (1 - clamp_margin_percent(margin) / 100))


def resolve_unit_price(line: Mapping[str, Any]) -> float:
    std, margin = line.get("stdCost"), line.get("marginPercent")
    if std is not None and std > 0 and margin is not None:
        return derive_unit_price(std, margin)
    return quote_round2(_nc(line.get("unitPrice"), 0))


def line_total(unit_price: float, quantity: Any) -> float:
    return quote_round2(unit_price * (quantity if _finite(quantity) else 0))


def compute_quote_totals(line_totals: Sequence[Any], tax_rate_percent: Any = 0) -> dict[str, float]:
    """quote/totals.ts ``computeQuoteTotals``."""
    total = 0.0
    for value in line_totals:
        total = total + (value if _truthy(value) else 0)
    subtotal = quote_round2(total)
    tax = quote_round2(subtotal * (clamp_margin_percent(tax_rate_percent) / 100))
    return {"subtotal": subtotal, "taxAmount": tax, "totalAmount": quote_round2(subtotal + tax)}


def fallback_matrix_unit_cost(engine_unit_cost: Any, line_std_cost: Any = None,
                              theoretical_fg_cost: Any = None,
                              customer_supplied_count: Any = None) -> dict[str, Any]:
    """quote/matrix-fallback.ts ``fallbackMatrixUnitCost`` (TASK-2175; G-D4, TASK-2611: a
    column whose material the customer sends keeps the engine's value, since a standard cost
    made at PLX prices would put a price back on material the quote prices at $0)."""
    if _finite(engine_unit_cost) and engine_unit_cost > 0:
        return {"unitCost": engine_unit_cost, "source": "engine"}
    if (customer_supplied_count or 0) > 0:
        return {"unitCost": 0, "source": "none"}
    if line_std_cost is not None and _finite(line_std_cost) and line_std_cost > 0:
        return {"unitCost": line_std_cost, "source": "line-std"}
    if theoretical_fg_cost is not None and _finite(theoretical_fg_cost) and theoretical_fg_cost > 0:
        return {"unitCost": theoretical_fg_cost, "source": "theoretical-fg"}
    return {"unitCost": 0, "source": "none"}


def first_positive_line_std_cost(lines: Sequence[Mapping[str, Any]]) -> Any:
    for line in lines:
        std = line.get("stdCost")
        if std is not None and _finite(std) and std > 0:
            return std
    return None


def reprice_margin_tiers(tiers: Sequence[Mapping[str, Any]], unit_cost: float) -> list[dict[str, Any]]:
    out = []
    for tier in tiers:
        m = tier["marginPct"] / 100 if 0 <= tier["marginPct"] < 100 else 0
        out.append({**tier, "sellPricePerUnit": _round(unit_cost / (1 - m), 100.0)})
    return out


def apply_column_unit_cost_fallback(column: Mapping[str, Any], line_std_cost: Any,
                                    theoretical_fg_cost: Any) -> dict[str, Any]:
    resolved = fallback_matrix_unit_cost(column["unitCost"], line_std_cost, theoretical_fg_cost,
                                         len(column.get("customerSupplied") or ()))
    if resolved["source"] in ("engine", "none"):
        return {"column": dict(column), "source": resolved["source"]}
    return {"source": resolved["source"],
            "column": {**column, "unitCost": resolved["unitCost"],
                       "marginTiers": reprice_margin_tiers(column["marginTiers"],
                                                           resolved["unitCost"])}}


def resolve_offer_margin_pct(column: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
                             quote_margin_target_pct: Any) -> float:
    """quote/matrix.ts ``resolveOfferMarginPct``."""
    explicit = next((s for s in selections if s["askedUnits"] == column["askedUnits"]), None)
    if explicit is not None and _finite(explicit.get("marginPct")):
        return explicit["marginPct"]
    if quote_margin_target_pct is not None and _finite(quote_margin_target_pct):
        return quote_margin_target_pct
    target = next((t for t in column["marginTiers"] if t["isTarget"]), None)
    return target["marginPct"] if target is not None else 0


def price_column_at_margin(column: Mapping[str, Any], margin_pct: Any) -> float:
    """quote/matrix.ts ``priceColumnAtMargin``: the ladder tier, else cost ÷ (1 − m)."""
    tier = next((t for t in column["marginTiers"] if abs(t["marginPct"] - margin_pct) <= 0.01),
                None)
    if tier is not None:
        return tier["sellPricePerUnit"]
    m = margin_pct / 100 if _finite(margin_pct) and 0 <= margin_pct < 100 else 0
    return _round(column["unitCost"] / (1 - m), 100.0)


def offer_line(asked_units: float, unit_cost: float, margin_pct: float,
               unit_price: float) -> dict[str, Any]:
    """quote/service.ts ``buildOfferLineCreates`` arithmetic for one saved asked-volume line."""
    price = quote_round2(unit_price)
    return {"quantity": asked_units, "unitPrice": price,
            "totalPrice": line_total(price, asked_units), "marginPercent": margin_pct,
            "stdCost": unit_cost}


# ---- ERP-D-015 posting (procurement/landed-gl.ts) ----------------------------------------------

def product_kind(product_type: Any) -> str:
    t = (product_type or "").strip().upper() if isinstance(product_type, str) or \
        product_type is None else str(product_type).strip().upper()
    if t in ("RAW_MATERIAL", "RAW"):
        return "RAW_MATERIAL"
    if t in ("PACKAGING", "PACK"):
        return "PACKAGING"
    if t in ("DISTRIBUTION", "RETAIL"):
        return "DISTRIBUTION"
    return "OTHER"


def predominant_kind(types: Sequence[Any]) -> str:
    counts = {"RAW_MATERIAL": 0, "PACKAGING": 0, "DISTRIBUTION": 0, "OTHER": 0}
    for t in types:
        counts[product_kind(t)] += 1
    best = "OTHER"
    for kind in ("RAW_MATERIAL", "PACKAGING", "DISTRIBUTION", "OTHER"):
        if counts[kind] > counts[best]:
            best = kind
    return best


def build_landed_expense_lines(product_types: Sequence[Any], freight_amount: Any = None,
                               duty_amount: Any = None) -> list[dict[str, Any]]:
    """landed-gl.ts ``buildLandedExpenseLines``: freight and duty post as G/L Account lines."""
    kind = predominant_kind(product_types)
    out = []
    freight = js_number(freight_amount)
    if freight is not None and math.isfinite(freight) and freight > 0:
        out.append({"lineType": "Account", "quantity": 1, "unitCost": _round(freight, 100.0),
                    "accountNumber": LANDED_GL["packFreight"] if kind == "PACKAGING"
                    else LANDED_GL["rmFreight"], "description": f"Freight ({kind})"})
    duty = js_number(duty_amount)
    if duty is not None and math.isfinite(duty) and duty > 0:
        account = {"PACKAGING": LANDED_GL["packDuty"],
                   "DISTRIBUTION": LANDED_GL["distDuty"]}.get(kind, LANDED_GL["rmDuty"])
        out.append({"lineType": "Account", "accountNumber": account, "quantity": 1,
                    "unitCost": _round(duty, 100.0),
                    "description": f"Duties & brokerage ({kind})"})
    return out


def _positive(value: Any) -> bool:
    """A finite number above zero (resolvePostedLandedAmounts' test for an actual)."""
    number = js_number(value)
    return number is not None and math.isfinite(number) and number > 0


def resolve_posted_landed_amounts(receipt_freight: Any = None, receipt_duty: Any = None,
                                  po_freight: Any = None, po_duty: Any = None) -> dict[str, float]:
    """landed-gl.ts ``resolvePostedLandedAmounts``: a receipt actual above zero beats the PO
    estimate."""
    def pick(actual: Any, estimate: Any) -> float:
        a = js_number(actual)
        if a is not None and math.isfinite(a) and a > 0:
            return a
        e = js_number(estimate)
        return e if e is not None and math.isfinite(e) and e > 0 else 0.0
    return {"freight": pick(receipt_freight, po_freight), "duty": pick(receipt_duty, po_duty)}


# ---- labour hours, formula and ASSEMBLY BOM selection, raw cost (LOG-08) -----------------------
#
# Record lists are reads: a list (possibly empty) is what the table holds; None is a read that
# did not happen, reproduced as the Portal's failed read (formula pin: pin_unresolved; routing,
# draft costs: none; setup standards: the workbook seed; the ASSEMBLY BOM link, whose failed
# read the Portal does not catch: skipped). quote_landed reports each as RECORDS_NOT_READ.

def _pos(value: Any) -> Any:
    """``positiveOrNull``: a finite number above zero, else None."""
    return value if _finite(value) and value > 0 else None


def _gt0(value: Any) -> bool:
    """``value != null && value > 0`` for the numbers a Prisma row holds."""
    return _is_num(value) and value > 0


def _newest(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Prisma ``orderBy: { updatedAt: "desc" }`` (stable for equal keys)."""
    return sorted(rows, key=lambda row: _iso(row.get("updatedAt")) or "", reverse=True)


def labour_cost_per_unit(hours_per_unit: Any, assembly_rate: Any = None) -> float | None:
    """fg-routing.ts ``labourCostPerUnit``."""
    hours = _pos(hours_per_unit)
    rate = _pos(_nc(assembly_rate, RATE_DEFAULTS["assemblyRate"]))
    return None if hours is None or rate is None else hours * rate


def amortise_setup_hours(setup_minutes: Any, changeover_minutes: Any, charges: Any,
                         asked_units: Any, crew: Any = None) -> dict[str, Any]:
    """setup-standards.ts ``amortiseSetupHours``: setup once per run and changeover once per
    charge beyond the first, in person-hours (× crew, default 1), over the asked units."""
    reasons: list[str] = []
    people = _nc(_pos(crew), 1)
    count = max(1, _js_math_round(_nc(_pos(charges), 1)))
    setup = _pos(setup_minutes)
    if setup is None:
        reasons.append("setup-minutes-missing")
    changeover = _pos(changeover_minutes)
    if changeover is None and count > 1:
        reasons.append("changeover-minutes-missing-multi-charge")
    per_run = (_nc(setup, 0) * people) / 60
    per_charge = (_nc(changeover, 0) * people) / 60
    qty = _pos(asked_units)
    if qty is None:
        reasons.append("asked-units-missing-no-amortisation-basis")
    per_unit = None if qty is None else (per_run + per_charge * (count - 1)) / qty
    return {"setupHoursPerUnit": per_unit, "perRunSetupHours": per_run,
            "perChargeChangeoverHours": per_charge,
            "charges": count, "reasons": reasons}


def compounding_crew_hours_per_unit(compounder_count: Any, hours_per_charge: Any, charges: Any,
                                    asked_units: Any) -> float | None:
    """setup-standards.ts ``compoundingCrewHoursPerUnit``: crew × hours per charge × charges
    over the asked units."""
    crew, hours, count, qty = (_pos(compounder_count), _pos(hours_per_charge), _pos(charges),
                               _pos(asked_units))
    if crew is None or hours is None or count is None or qty is None:
        return None
    return (crew * hours * count) / qty


def quote_labour_from_routing(routing: Mapping[str, Any] | None, assembly_rate: Any = None,
                              run_qty: Any = None) -> dict[str, Any]:
    """fg-routing.ts ``quoteLabourFromRouting``: the routing's standard hours plus its setup
    amortised over one charge of the run quantity."""
    unrouted = {"status": "unrouted", "assemblyHoursPerUnit": None, "assemblyLabourPerUnit": None}
    if routing is None:
        return dict(unrouted, reason="no-routing")
    base = _pos(routing.get("standardHoursPerUnit"))
    if base is None:
        return dict(unrouted, reason="hours-missing")
    hours = base
    setup, crew, qty = (_pos(routing.get("standardSetupMinutes")),
                        _pos(routing.get("totalOperators")), _pos(run_qty))
    if setup is not None and crew is not None and qty is not None:
        hours = hours + _nc(amortise_setup_hours(setup, None, 1, qty, crew)["setupHoursPerUnit"], 0)
    return {"status": "routed", "assemblyHoursPerUnit": hours,
            "assemblyLabourPerUnit": labour_cost_per_unit(hours, assembly_rate), "reason": "ok"}


def assembly_labour_per_unit(headcount: Any, rate_bpm: Any) -> float:
    """template-cost-matrix.ts ``assemblyLabourPerUnit``: headcount ÷ (BPM × 60) hours/unit."""
    if not (_gt0(headcount) and _gt0(rate_bpm)):
        raise LandedCostError("assembly labour requires positive headcount and rateBpm")
    return headcount / (rate_bpm * 60)


def compounding_labour_per_unit(compounder_count: Any, hours: Any, yield_units: Any) -> float:
    """template-cost-matrix.ts ``compoundingLabourPerUnit``: compounders × shift hours ÷ yield."""
    if not (_gt0(compounder_count) and _gt0(hours) and _gt0(yield_units)):
        raise LandedCostError("compounding labour requires positive compounders, hours, and yield")
    return (compounder_count * hours) / yield_units


_SETUP_RANK = {"confirmed": 0, "draft": 1, "inferred": 2, "excel": 3, "seed": 4}
_DRAFT_RANK = {"confirmed": 0, "draft": 1, "seed": 4}
_PROVENANCE_RANK = {"confirmed": 0, "draft": 1, "excel": 2, "inferred": 3}  # line-tank-keys.ts
# setup-standards.ts workbookSeedRow: the workbook wedge (6.5 h uptime - 4 h productive setup,
# 7.5 h paid - 6.5 h uptime changeover), the Portal's code default. Never mutated.
WORKBOOK_SETUP_SEED = {"lineNumber": 0, "groupCode": "*", "actionKey": "*",
                       "setupMinutes": (6.5 - 4) * 60, "changeoverMinutes": (7.5 - 6.5) * 60,
                       "source": "seed"}
RUN_VOLUME_TIER_CODES = tuple(code for code, _ in RUN_VOLUME_TIERS)
DEFAULT_RUN_VOLUME_TIER_CODE = "5k"


def _rank(source: Any, table: Mapping[str, int]) -> int:
    """``setupProvenanceRank`` / ``draftCostProvenanceRank``, then line-tank ``provenanceRank``."""
    if not isinstance(source, str) or not source:
        return 99
    key = source.lower()
    return table[key] if key in table else _PROVENANCE_RANK.get(key, 50)


def _js_max(values: Sequence[Any]) -> float:
    """``Math.max(...values)`` over row minutes (null reads as 0)."""
    numbers = [0 if v is None else v for v in values]
    if not all(_is_num(v) for v in numbers):
        raise LandedCostError("setup_standard_minutes_not_numeric")
    return max(numbers)


def resolve_setup_standard(rows: Sequence[Mapping[str, Any]], *, line_number: Any = None,
                           group_code: Any = None, action_keys: Any = None) -> dict[str, Any]:
    """setup-standards.ts ``resolveSetupStandard``: confirmed beats draft beats inferred beats
    excel/seed, then the most specific key (line, group, action), then the largest minutes;
    always falls back to the workbook seed."""
    line = line_number if _gt0(line_number) else None
    group = group_code.strip() if isinstance(group_code, str) and group_code.strip() else None
    keys = [k.strip() for k in action_keys or [] if isinstance(k, str) and k.strip()]

    def matches(row: Mapping[str, Any]) -> bool:
        at_line, at_group = row.get("lineNumber"), row.get("groupCode")
        return (at_line == 0 or (line is not None and at_line == line)) \
            and (at_group == "*" or (group is not None and at_group == group)) \
            and (row.get("actionKey") == "*" or row.get("actionKey") in keys)

    def specificity(row: Mapping[str, Any]) -> int:
        return (4 if row.get("lineNumber") != 0 else 0) \
            + (2 if row.get("groupCode") != "*" else 0) + (1 if row.get("actionKey") != "*" else 0)

    catalog = list(rows) or [WORKBOOK_SETUP_SEED]
    pool = [row for row in catalog if matches(row)] or [WORKBOOK_SETUP_SEED]
    best_rank, best_spec = math.inf, -1
    for row in pool:
        rank, spec = _rank(row.get("source"), _SETUP_RANK), specificity(row)
        if rank < best_rank or (rank == best_rank and spec > best_spec):
            best_rank, best_spec = rank, spec
    winners = [row for row in pool if _rank(row.get("source"), _SETUP_RANK) == best_rank
               and specificity(row) == best_spec]
    return {"setupMinutes": _js_max([row.get("setupMinutes") for row in winners]),
            "changeoverMinutes": _js_max([row.get("changeoverMinutes") for row in winners]),
            "source": _nc(winners[0].get("source"), "seed"), "matched": winners}


def primary_routing(fg_routings: Sequence[Mapping[str, Any]] | None, project_key: Any,
                    setup_standards: Sequence[Mapping[str, Any]] | None
                    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``getPrimaryRouting(key, {persist: false})`` (refreshPrimaryRoutingSetup): the primary
    FgRouting of the project, its blank setup/changeover minutes filled, unsaved, from the setup
    standard of its line, group and actions. Returns (routing, fill): fill names the filled
    fields and the standard that filled them, None when nothing was filled."""
    key = project_key.strip() if isinstance(project_key, str) else ""
    if not key or fg_routings is None:
        return None, None
    row = next((r for r in fg_routings
                if r.get("projectKey") == key and r.get("isPrimary") is True), None)
    if row is None:
        return None, None
    if row.get("standardSetupMinutes") is not None and \
            row.get("standardChangeoverMinutes") is not None:
        return dict(row), None
    actions = row.get("actionKeys")
    standard = resolve_setup_standard(
        setup_standards if setup_standards is not None else [WORKBOOK_SETUP_SEED],
        line_number=row.get("lineNumber"), group_code=row.get("groupCode"),
        action_keys=[k for k in actions if isinstance(k, str)] if isinstance(actions, list) else [])
    fields = {"standardSetupMinutes": "setupMinutes",
              "standardChangeoverMinutes": "changeoverMinutes"}
    filled = [f for f in fields if row.get(f) is None]
    return dict(row, **{f: _nc(row.get(f), standard[m]) for f, m in fields.items()}), \
        {"fields": filled, "standard": standard}


_DRAFT_FIELDS = ("tankCode", "batchType", "compounderCount", "hours", "yieldUnits", "labourPerUnit",
                 "lineNumber", "lineCode", "headcount", "rateBpm", "uptimeHours", "setupMinutes",
                 "teardownMinutes", "overheadPerUnit")


def draft_cost_rows(rows: Sequence[Mapping[str, Any]] | None,
                    project_key: Any) -> list[dict[str, Any]] | None:
    """``loadDraftCostStore(key).drafts`` (toDraftRow): the project's draft cost override rows;
    a row without a run-volume tier is skipped; an unknown source reads as ``seed``. None when
    the project has no key or the rows were not read."""
    key = project_key.strip() if isinstance(project_key, str) else ""
    if not key or rows is None:
        return None
    out = []
    for row in rows:
        if row.get("projectKey") != key:
            continue
        code = (row.get("runVolumeTier") or {}).get("code")
        if not _truthy(code):
            continue
        tier = code.strip() if isinstance(code, str) else None
        source = row.get("source")
        out.append({"projectKey": row.get("projectKey"), "fgItemId": row.get("fgItemId"),
                    "process": "assembly" if row.get("process") == "assembly" else "compounding",
                    "runVolumeTierCode": tier if tier in RUN_VOLUME_TIER_CODES
                    else DEFAULT_RUN_VOLUME_TIER_CODE,
                    "templateId": row.get("templateId"),
                    "source": source if source in ("confirmed", "draft") else "seed",
                    **{f: row.get(f) for f in _DRAFT_FIELDS}, "_source": row.get("_source")})
    return out


def resolve_draft_cost(drafts: Sequence[Mapping[str, Any]] | None, project_key: Any,
                       process: str, tier_code: Any) -> dict[str, Any] | None:
    """draft-cost-overrides.ts ``resolveDraftCost`` (no template candidate, as the engine asks):
    the project's row for the process and tier with the best provenance."""
    candidates = [row for row in drafts or [] if row.get("projectKey") == project_key
                  and row.get("process") == process and row.get("runVolumeTierCode") == tier_code]
    if not candidates:
        return None
    return dict(sorted(candidates, key=lambda row: _rank(row.get("source"), _DRAFT_RANK))[0])


def _draft_assembly_hours(row: Mapping[str, Any]) -> float | None:
    if _gt0(row.get("labourPerUnit")):
        return row["labourPerUnit"]
    if _gt0(row.get("headcount")) and _gt0(row.get("rateBpm")):
        return assembly_labour_per_unit(row["headcount"], row["rateBpm"])
    return None


def _draft_compounding_hours(row: Mapping[str, Any]) -> float | None:
    if _gt0(row.get("labourPerUnit")):
        return row["labourPerUnit"]
    if _gt0(row.get("compounderCount")) and _gt0(row.get("hours")) and _gt0(row.get("yieldUnits")):
        return compounding_labour_per_unit(row["compounderCount"], row["hours"], row["yieldUnits"])
    return None


def resolve_quote_hours(project_key: Any, drafts: Sequence[Mapping[str, Any]] | None,
                        routing: Mapping[str, Any] | None, run_qty: Any, fg_target_qty: Any,
                        assembly_rate: Any) -> dict[str, Any]:
    """project-landed-quote.ts ``resolveQuoteHours``: draft tier rows win for the priced tier
    (assembly from labourPerUnit or headcount ÷ BPM, plus setup amortised over the run;
    compounding from labourPerUnit or crew × shift ÷ yield); the primary routing is the fallback
    per process; neither is ``missing`` and reported, never defaulted."""
    reasons: list[str] = []
    rows = [row for row in drafts or [] if row.get("projectKey") == project_key]
    has_drafts = len(rows) > 0
    qty_basis = _nc(run_qty, fg_target_qty)
    tier = None
    if has_drafts:
        tier = pick_run_volume_tier(qty_basis)
        if not tier:
            tier = DEFAULT_RUN_VOLUME_TIER_CODE
            reasons.append("tier-defaulted-no-qty-basis")
    sources = {"assembly": "missing", "compounding": "missing"}
    run_hours = assembly_hours = compounding_hours = None
    if tier and drafts is not None and project_key:
        assembly = resolve_draft_cost(drafts, project_key, "assembly", tier)
        compounding = resolve_draft_cost(drafts, project_key, "compounding", tier)
        hours = _draft_assembly_hours(assembly) if assembly else None
        if hours is not None:
            sources["assembly"] = "draft_tier"
            run_hours = assembly_hours = hours
            if _gt0(assembly.get("setupMinutes")) and _gt0(assembly.get("headcount")) and \
                    _gt0(qty_basis):
                assembly_hours = assembly_hours + _nc(amortise_setup_hours(
                    assembly["setupMinutes"], None, 1, qty_basis,
                    assembly["headcount"])["setupHoursPerUnit"], 0)
        hours = _draft_compounding_hours(compounding) if compounding else None
        if hours is not None:
            sources["compounding"] = "draft_tier"
            compounding_hours = hours
    routed = quote_labour_from_routing(routing, assembly_rate, run_qty)
    usable = routed["status"] == "routed" and routed["assemblyHoursPerUnit"] is not None
    if sources["assembly"] == "missing":
        if usable:
            sources["assembly"] = "routing"
            run_hours = _nc((routing or {}).get("standardHoursPerUnit"),
                            routed["assemblyHoursPerUnit"])
            assembly_hours = routed["assemblyHoursPerUnit"]
        else:
            reasons.append(f"assembly-hours-missing:{routed['reason']}")
    if sources["compounding"] == "missing":
        if usable:
            sources["compounding"] = "routing"
            compounding_hours = routed["assemblyHoursPerUnit"]
        else:
            reasons.append(f"compounding-hours-missing:{routed['reason']}")
    same = sources["assembly"] == sources["compounding"]
    return {"labour": {"source": sources["assembly"] if same else "mixed",
                       "assemblySource": sources["assembly"],
                       "compoundingSource": sources["compounding"], "pricedTierCode": tier,
                       "pricedQtyBasis": qty_basis, "assemblyRunHoursPerUnit": run_hours,
                       "assemblyHoursPerUnit": assembly_hours,
                       "compoundingHoursPerUnit": compounding_hours, "reasons": reasons},
            "hasDraftOverrides": has_drafts}


def resolve_expected_units_override(drafts: Sequence[Mapping[str, Any]] | None, project_key: Any,
                                    priced_tier_code: Any) -> dict[str, Any] | None:
    """project-landed-quote.ts ``resolveExpectedUnitsOverride``: the priced assembly tier row's
    expected units per shift (yieldUnits) and its up-time hours, for the OD-14 absorption."""
    if drafts is None or not project_key or not priced_tier_code:
        return None
    row = resolve_draft_cost(drafts, project_key, "assembly", priced_tier_code)
    if not row or not _gt0(row.get("yieldUnits")):
        return None
    return {"expectedUnitsPerShift": row["yieldUnits"],
            "uptimeHours": row["uptimeHours"] if _gt0(row.get("uptimeHours")) else None}


def volume_column_labour(asked_units: Any, project_key: Any,
                         drafts: Sequence[Mapping[str, Any]] | None,
                         routing: Mapping[str, Any] | None,
                         setup_standard: Mapping[str, Any] | None, charges: Any,
                         assembly_rate: Any, rates: Mapping[str, Any] | None) -> dict[str, Any]:
    """The hours of ``buildVolumeMatrixColumn`` at one asked volume: W2b hours at that volume,
    setup minutes from the tier's draft row, else the routing, else the setup standard;
    changeover from the setup standard per charge beyond the first; compounding from the
    draft crew per charge when a vessel pick exists, else the per-unit hours. ``charges``: the
    vessel pick's charge count (an input; None = no pick, one charge assumed)."""
    count = _nc(charges, 1)
    base = resolve_quote_hours(project_key, drafts, routing, asked_units, None,
                               assembly_rate)["labour"]
    reasons = list(base["reasons"])
    tier = _nc(base["pricedTierCode"], pick_run_volume_tier(asked_units))
    resolvable = drafts is not None and project_key and tier
    assembly = resolve_draft_cost(drafts, project_key, "assembly", tier) if resolvable else None
    compounding = resolve_draft_cost(drafts, project_key, "compounding", tier) \
        if resolvable else None
    setup_minutes, setup_source = None, "missing"
    if assembly and _gt0(assembly.get("setupMinutes")):
        setup_minutes, setup_source = assembly["setupMinutes"], "draft_tier"
    elif routing and _gt0(routing.get("standardSetupMinutes")):
        setup_minutes, setup_source = routing["standardSetupMinutes"], "routing"
    elif setup_standard:
        setup_minutes = setup_standard["setupMinutes"]
        setup_source = f"setup-standard:{setup_standard['source']}"
    changeover = (setup_standard or {}).get("changeoverMinutes")
    crew = _nc((assembly or {}).get("headcount"), (routing or {}).get("totalOperators"))
    assembly_hours = None
    if base["assemblyRunHoursPerUnit"] is not None:
        amortised = amortise_setup_hours(setup_minutes, changeover, count, asked_units, crew)
        reasons += [r for r in amortised["reasons"] if r not in reasons]
        assembly_hours = base["assemblyRunHoursPerUnit"] + _nc(amortised["setupHoursPerUnit"], 0)
    compounding_hours, basis = None, "missing"
    per_charge = compounding_crew_hours_per_unit(
        (compounding or {}).get("compounderCount"), (compounding or {}).get("hours"), count,
        asked_units) if charges is not None else None
    if per_charge is not None:
        compounding_hours, basis = per_charge, "crew_per_charge"
    elif base["compoundingHoursPerUnit"] is not None:
        compounding_hours, basis = base["compoundingHoursPerUnit"], "per_unit_fallback"
        if count > 1:
            reasons.append("compounding-hours-per-unit-basis-no-charge-scaling "
                           "(crew-per-charge facts missing)")
    else:
        reasons.append("compounding-hours-missing-for-volume")
    override = resolve_expected_units_override(drafts, project_key, base["pricedTierCode"])
    return {"labour": {"assemblySource": base["assemblySource"],
                       "compoundingSource": "missing" if basis == "missing"
                       else base["compoundingSource"],
                       "tierCode": tier, "assemblyRunHoursPerUnit": base["assemblyRunHoursPerUnit"],
                       "assemblyHoursPerUnit": assembly_hours,
                       "compoundingHoursPerUnit": compounding_hours, "compoundingBasis": basis,
                       "setupSource": setup_source, "reasons": reasons},
            "overhead": _overhead(rates, assembly_hours, base["assemblySource"], override)}


def raw_cost_per_kg(product: Mapping[str, Any], supply_source: str = "PLX") -> Any:
    """raw-cost.ts ``rawCostPerKg``: 0 when the customer supplies the raw (G9), else the FM
    average cost (Product.unitCost) when above 0, else the first preferred active card's
    systemCost (CAD per kg) above 0, else 0."""
    if supply_source != "PLX":
        return 0
    if _pos(product.get("unitCost")) is not None:
        return product["unitCost"]
    card = _raw_cost_card(product.get("supplierProducts") or [])
    return card["systemCost"] if card is not None else 0


def _raw_cost_card(cards: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((c for c in cards if _truthy(c.get("isPreferred")) and _truthy(c.get("isActive"))
                 and _pos(c.get("systemCost")) is not None), None)


def _raw_cost_cards(product: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``RAW_COST_CARD_INCLUDE``: active preferred cards, newest first."""
    return _newest([c for c in product.get("supplierProducts") or []
                    if c.get("isActive") is True and c.get("isPreferred") is True])


def quote_raw_cost_per_kg(product: Mapping[str, Any], supply_source: str = "PLX") -> Any:
    """gatherQuoteFacts' ``costPerKg``: rawCostPerKg over the cards the Portal's include loads."""
    return raw_cost_per_kg(dict(product, supplierProducts=_raw_cost_cards(product)), supply_source)


def bare_project_key(project_key: str) -> str:
    """overview-shared.ts ``bareProjectKey``: ``fm-proj-77`` -> ``77``."""
    key = project_key.strip()
    if not key.startswith("fm-proj-"):
        return key
    return key[len("fm-proj-"):] or key


def resolve_quote_formulation(project_key: Any, project_id: Any,
                              subassemblies: Sequence[Mapping[str, Any]] | None,
                              fg_items: Sequence[Mapping[str, Any]] | None,
                              formulations: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """formulation-resolution.ts ``resolveQuoteFormulation``: the project pin (the newest formula
    subassembly row of the project's id, key or bare key), else the FG pin, else none; the pinned
    formulation must exist, be undeleted and not OBSOLETE. A failed read, or a pin that cannot
    be loaded, is ``pin_unresolved``: never a fallthrough to another formula. ``pin``: the record
    that pinned it."""
    key = project_key.strip() if isinstance(project_key, str) and project_key.strip() else None
    unresolved = {"formulation": None, "source": "pin_unresolved", "pin": None}
    pin_keys = list(dict.fromkeys(
        k for k in (project_id.strip() if isinstance(project_id, str) else None, key,
                    bare_project_key(key) if key else None) if k))
    pin = None
    if pin_keys:
        if subassemblies is None:
            return unresolved
        rows = _newest([r for r in subassemblies if r.get("kind") == "formula"
                        and r.get("fmProjectId") in pin_keys
                        and r.get("formulationId") is not None])
        pin = rows[0] if rows else None
    source, pinned = "project_pin", (pin or {}).get("formulationId")
    if not pinned and key:
        if fg_items is None:
            return unresolved
        pin = next((r for r in fg_items if r.get("projectKey") == key), None)
        source, pinned = "fg_pin", (pin or {}).get("pinnedFormulationId")
    if not pinned:
        return {"formulation": None, "source": "none", "pin": None}
    if formulations is None:
        return dict(unresolved, pin=pin)
    row = next((f for f in formulations if f.get("id") == pinned and f.get("status") is not None
                and f.get("status") != "OBSOLETE" and f.get("deletedAt") is None), None)
    if row is None:
        return dict(unresolved, pin=pin)
    return {"formulation": row, "source": source, "pin": pin}


def select_assembly_bom(project: Mapping[str, Any],
                        subassemblies: Sequence[Mapping[str, Any]] | None,
                        boms: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """project-landed-quote.ts ``findAssemblyBomForQuote``: the BOM the project's newest assembly
    subassembly row pins (ASSEMBLY, undeleted), else the newest-version ASSEMBLY BOM stamped with
    the project's key or id (or gold id). Returns ``{"bom", "link"}``."""
    keys = list(dict.fromkeys(k for k in (project.get("id"), project.get("projectKey")) if k))
    live = [b for b in boms or [] if b.get("category") == "ASSEMBLY" and b.get("deletedAt") is None]
    if keys and subassemblies is not None:
        links = _newest([r for r in subassemblies if r.get("kind") == "assembly"
                         and r.get("fmProjectId") in keys and r.get("bomId") is not None])
        if links and _truthy(links[0].get("bomId")):
            pinned = next((b for b in live if b.get("id") == links[0]["bomId"]), None)
            if pinned is not None:
                return {"bom": pinned, "link": links[0]}
    stamped = []
    if project.get("projectKey"):
        stamped.append(("projectKey", project["projectKey"]))
    if project.get("id"):
        stamped.append(("projectId", project["id"]))
    if project.get("goldId") and project.get("goldId") != project.get("id"):
        stamped.append(("projectId", project["goldId"]))
    rows = [b for b in live if any(b.get(field) == value for field, value in stamped)]
    rows = sorted(_newest(rows), key=lambda b: b.get("version") if _is_num(b.get("version")) else 0,
                  reverse=True)
    return {"bom": rows[0] if rows else None, "link": None}


# ---- G3 supply source: who supplies a component (lib/customer-materials) ------------------------

SUPPLY_SOURCES = ("PLX", "CUSTOMER", "CUSTOMER_PENDING")
CUSTOMER_SOURCING_PATH = "CUSTOMER"
SOURCING_CLOSED_STATUSES = ("COMPLETED", "CANCELLED", "LINKED")  # sourcing/status.ts
_ANCHOR_PREFIXES = ("fm-cust-", "local-cust-")  # customers/anchor.ts FM_/LOCAL_ANCHOR_PREFIX
_FM_RAW_ID = re.compile(r"fm-raw-([0-9]+)", re.I)
_FM_RAW_EXTERNAL_ID = re.compile(r"(?:fm-raw-)?([0-9]+)", re.I)
# fact key -> the Portal relation the supply-source read selects from. Read only when the quote
# asks about a product or BOM line, as the Portal reads it; a needed list that is None is the
# Portal's failed read, which stops the quote (SupplySourceReadError), never PLX pricing.
SUPPLY_RELATIONS = {"customers": "app_customer", "products": "app_product",
                    "sourcing_requests": "app_sourcing_request",
                    "customer_supplied_items": "app_customer_supplied_item"}


def _id_text(value: Any) -> str:
    """``value?.trim()`` of an id: "" for null; anything but text fails closed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LandedCostError("supply_source_id_must_be_text")
    return js_trim(value)


def _unique_ids(ids: Sequence[Any]) -> list[str]:
    """supply-source.ts ``uniqueIds``: trimmed, blank dropped, first occurrence kept."""
    return list(dict.fromkeys(t for t in map(_id_text, ids) if t))


def supply_source_from_facts(registry: str, requests: Sequence[Mapping[str, Any]]) -> str:
    """supply-source-shared.ts ``supplySourceFromFacts``: an open CUSTOMER-path request is
    CUSTOMER_PENDING (an unknown status counts as open), else an ACTIVE registry row is
    CUSTOMER, else PLX."""
    if any(r.get("status") not in SOURCING_CLOSED_STATUSES for r in requests):
        return "CUSTOMER_PENDING"
    return "CUSTOMER" if registry == "ACTIVE" else "PLX"


def raw_material_id_for_product(product: Mapping[str, Any]) -> str:
    """raw-product.ts ``rawMaterialIdForProduct``: the FileMaker raw id of the external id
    (``fm-raw-<n>`` or a bare ``<n>``), else of an ``fm-raw-<n>`` product id, else the id."""
    external = product.get("externalId")
    match = _FM_RAW_EXTERNAL_ID.fullmatch(js_trim(external)) if isinstance(external, str) \
        else None
    match = match or _FM_RAW_ID.fullmatch(js_trim(product["id"]))
    return match.group(1) if match else product["id"]


def resolve_supply_sources(customer_key: str, product_ids: Sequence[Any],
                           bom_lines: Sequence[Mapping[str, Any]],
                           facts: Mapping[str, Any]) -> dict[str, Any]:
    """supply-source.ts ``resolveSupplySources`` for one customer (its unified key, resolved
    through its gold anchor) over the records it reads (``SUPPLY_RELATIONS``): PLX, CUSTOMER or
    CUSTOMER_PENDING per product (formula raws) and per BOM line. ``records``: the anchor,
    registry rows and requests that decided each answer, keyed ``product:<id>`` / ``line:<id>``."""
    pids = _unique_ids(product_ids)
    lids = _unique_ids([line.get("id") for line in bom_lines])
    out: dict[str, Any] = {"byProductId": {}, "byBomLineId": {}, "records": {}}
    if not pids and not lids:
        return out

    def read(name: str) -> list[Mapping[str, Any]]:
        rows = facts.get(name)
        if rows is None:
            raise LandedCostError(f"supply_source_read_failed:{SUPPLY_RELATIONS[name]}")
        return list(rows)

    key = _id_text(customer_key)
    anchor = None
    if key:
        candidates = [prefix + key for prefix in _ANCHOR_PREFIXES]
        anchor = next((c for c in read("customers") if c.get("deletedAt") is None and (
            c.get("externalId") in candidates or c.get("id") in [key, *candidates])), None)
    gold_id = anchor["id"] if anchor else None
    line_product = {line.get("id"): line.get("productId") for line in bom_lines
                    if line.get("id") in lids}
    requests = read("sourcing_requests") if lids else []
    raw_products = [p for p in read("products") if p.get("id") in pids
                    and p.get("productType") == "RAW_MATERIAL"] if pids else []
    registry_ids = _unique_ids([*pids, *line_product.values()])
    rows = [r for r in read("customer_supplied_items") if r.get("customerId") == gold_id
            and r.get("productId") in registry_ids] if gold_id and registry_ids else []
    registry = {r["productId"]: ("ACTIVE" if _truthy(r.get("isActive")) else "STOPPED", r)
                for r in rows}
    raw_requests: dict[str, list[Mapping[str, Any]]] = {}
    keys = _unique_ids([customer_key, gold_id]) if raw_products else []
    if keys:
        by_raw_id: dict[str, list[str]] = {}
        for product in raw_products:
            by_raw_id.setdefault(raw_material_id_for_product(product), []).append(product["id"])
        raw_ids = [product["id"] for product in raw_products]
        for request in read("sourcing_requests"):
            if not (request.get("materialType") == "RAW"
                    and request.get("sourcingPath") == CUSTOMER_SOURCING_PATH
                    and request.get("status") is not None  # SQL NOT IN drops a null
                    and request.get("status") not in SOURCING_CLOSED_STATUSES
                    and request.get("customerId") in keys
                    and (request.get("linkedProductId") in raw_ids
                         or request.get("rawMaterialId") in by_raw_id)):
                continue
            named = list(by_raw_id.get(request.get("rawMaterialId"), [])) \
                if request.get("rawMaterialId") else []
            if request.get("linkedProductId") in raw_ids:
                named.append(request["linkedProductId"])
            for product_id in dict.fromkeys(named):
                raw_requests.setdefault(product_id, []).append(request)
    by_line: dict[str, list[Mapping[str, Any]]] = {}
    for request in requests:
        if request.get("bomLineId") in lids and \
                request.get("sourcingPath") == CUSTOMER_SOURCING_PATH:
            by_line.setdefault(request["bomLineId"], []).append(request)

    def answer(product_id: Any, asked: list[Mapping[str, Any]], record_key: str) -> str:
        state, row = registry.get(product_id, ("NONE", None)) if product_id else ("NONE", None)
        source = supply_source_from_facts(state, asked)
        basis = asked if source == "CUSTOMER_PENDING" else [anchor, row] if row else []
        out["records"][record_key] = [r for r in basis if r is not None]
        return source

    for product_id in pids:
        out["byProductId"][product_id] = answer(product_id, raw_requests.get(product_id, []),
                                                "product:" + product_id)
    for line_id in lids:
        out["byBomLineId"][line_id] = answer(line_product.get(line_id), by_line.get(line_id, []),
                                             "line:" + line_id)
    return out


def _supply_for(sources: Mapping[str, str], id_: Any) -> str:
    """project-landed-quote.ts ``supplySourceFor``: an id never asked about (blank) is PLX."""
    key = _id_text(id_)
    return (sources.get(key) if key else None) or "PLX"


def _round_qty(value: float) -> float:
    """project-landed-quote.ts ``roundQty``: ``Number(n.toPrecision(10))`` (exact decimal,
    ties away from zero, as toPrecision rounds)."""
    if not math.isfinite(value) or value == 0:
        return value
    exact = Decimal(value)
    return float(exact.quantize(Decimal(1).scaleb(exact.adjusted() - 9), rounding=ROUND_HALF_UP))


def customer_supplied_requirements(materials: Sequence[Mapping[str, Any]],
                                   units: Any) -> list[dict[str, Any]]:
    """project-landed-quote.ts ``customerSuppliedRequirements``: the quantity the customer must
    send for ``units`` finished units (null without a unit basis or a per-unit quantity)."""
    basis = units if _finite(units) and units > 0 else None
    return [dict(m, requiredQty=_round_qty(basis * m["qtyPerUnit"])
                 if basis is not None and m["qtyPerUnit"] is not None else None)
            for m in materials]


# ---- the engine: quoteProjectLanded over supplied facts ----------------------------------------

def _active_cards(product: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Prisma include of gatherQuoteFacts: ``supplierProducts where isActive, orderBy
    isPreferred desc, updatedAt desc``; tiers ``orderBy minQty asc``."""
    if not product:
        return []
    cards = [c for c in product.get("supplierProducts") or [] if c.get("isActive") is True]
    cards.sort(key=lambda c: _iso(c.get("updatedAt")) or "", reverse=True)
    cards.sort(key=lambda c: c.get("isPreferred") is True, reverse=True)
    return cards


def _tiers(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(({"minQty": t.get("minQty"), "unitCost": t.get("unitCost"),
                    "effectiveFrom": t.get("effectiveFrom"), "updatedAt": t.get("updatedAt"),
                    "_source": t.get("_source")} for t in card.get("tiers") or []),
                  key=lambda t: t["minQty"])


# fact key -> the Portal relation its records come from (a None list is a read that did not
# happen; quote_landed reports it as RECORDS_NOT_READ)
QUOTE_RELATIONS = {"project_subassemblies": "app_project_subassembly", "fg_items": "app_fg_item",
                   "formulations": "app_formulation", "boms": "app_bill_of_materials",
                   "fg_routings": "app_fg_routing",
                   "draft_cost_overrides": "app_draft_cost_override",
                   "setup_standards": "app_line_action_setup_standard"}


def quote_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """``gatherQuoteFacts`` over the records the Portal reads: ``project`` (the QuoteProjectRef:
    id, projectKey, goldId, customerId), the RateSettings row, project core (``target_price``,
    ``customer_id``), the record lists of ``QUOTE_RELATIONS`` that its queries select among
    (formula pin, FG item, formulations with ingredient products and their supplier cards,
    ASSEMBLY BOMs with lines/products/cards/tiers) and of ``SUPPLY_RELATIONS`` (G9: who supplies
    each BOM line and formula raw). A list is a read; None is a read that did not happen."""
    project = facts.get("project") or {}
    project_key = project.get("projectKey")
    subassemblies, fg_items = facts.get("project_subassemblies"), facts.get("fg_items")
    formulations = facts.get("formulations")
    resolved = resolve_quote_formulation(project_key, project.get("id"), subassemblies, fg_items,
                                         formulations)
    formulation = resolved["formulation"]
    sg_row = next((f for f in formulations or [] if f.get("id") == formulation["id"]), None) \
        if formulation else None
    selected = select_assembly_bom(project, subassemblies, facts.get("boms"))
    bom = selected["bom"]
    fg_key = project_key.strip() if isinstance(project_key, str) else ""
    fg = next((r for r in fg_items or [] if r.get("projectKey") == fg_key), None) \
        if fg_key else None
    lines_in = [line for line in (bom or {}).get("lines") or [] if line.get("deletedAt") is None]
    ingredients = (formulation or {}).get("ingredients") or []
    core = facts.get("project_core") or {}
    # G9: one supply-source batch for the project's customer, keyed as the sourcing writers key
    # it (the PD spine's core.customer_id), else the gold project's customer.
    supply = resolve_supply_sources(
        _id_text(core.get("customer_id")) or _id_text(project.get("customerId")),
        [ing.get("productId") for ing in ingredients], lines_in, facts)
    fill_weight_g = _nc((bom or {}).get("fillWeightG")) if bom else None
    specific_gravity = (sg_row or {}).get("specificGravity")
    mass = fill_mass_kg(fill_weight_g, specific_gravity)
    customer_supplied: list[dict[str, Any]] = []
    supply_basis: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    customer_raws: dict[Any, dict[str, Any]] = {}  # one entry per raw, inclusion summed
    raw = []
    for ing in ingredients:
        source = _supply_for(supply["byProductId"], ing.get("productId"))
        if source != "PLX":
            if not _finite(ing.get("percentage")):
                raise LandedCostError("customer_raw_inclusion_must_be_a_number")
            prior = customer_raws.get(ing["productId"])
            customer_raws[ing["productId"]] = {
                "name": _nc(prior["name"] if prior else None, ing["product"]["name"]),
                "supplySource": source,
                "inclusionPct": (prior["inclusionPct"] if prior else 0) + ing["percentage"]}
        raw.append({"name": ing["product"]["name"], "inclusionPct": ing.get("percentage"),
                    "costPerKg": quote_raw_cost_per_kg(ing["product"], source)})
    for product_id, entry in customer_raws.items():
        customer_supplied.append({
            "kind": "raw", "key": product_id, "name": entry["name"], "productId": product_id,
            "supplySource": entry["supplySource"],
            "qtyPerUnit": _round_qty((mass * entry["inclusionPct"]) / 100) if mass > 0 else None,
            "unit": "kg"})
        supply_basis[("raw", product_id)] = supply["records"]["product:" + _id_text(product_id)]
    counts = {"unsourced": 0, "linked": 0, "hs": 0, "incoterms": 0, "origin": 0,
              "customer": 0, "pending": 0}
    component_lines: list[dict[str, Any]] = []
    packaging: list[dict[str, Any]] = []
    for line in lines_in:
        product = line.get("product") or None
        name = _nc((product or {}).get("name"), line.get("packagingType"), line.get("id"))
        qty = line.get("quantity") if _truthy(line.get("quantity")) else 1
        linked = _truthy(line.get("productId")) and not _truthy(line.get("needsSourcing")) and \
            line.get("estimatedUnitCost") is None
        key = _nc(line.get("id"), name)
        source = _supply_for(supply["byBomLineId"], line.get("id"))
        if source != "PLX":
            # G9 (G-D4, G-D5): the customer sends it: $0 with no card, counted apart from
            # linked and unsourced lines, no HS, incoterms or origin check.
            counts["customer"] += 1
            counts["pending"] += source == "CUSTOMER_PENDING"
            customer_supplied.append({"kind": "packaging", "key": key, "name": name,
                                      "productId": _nc(line.get("productId")),
                                      "supplySource": source, "qtyPerUnit": qty, "unit": "each"})
            supply_basis[("packaging", key)] = supply["records"]["line:" + _id_text(line["id"])]
            component_lines.append({"lineKey": key, "lineName": name,
                                    "productId": _nc(line.get("productId")), "qtyPerUnit": qty,
                                    "linked": linked, "pricedUnitCost": 0, "card": None,
                                    "supplySource": source})
            packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": 0})
            continue
        if not linked:
            counts["unsourced"] += 1
            unit = _nc(line.get("estimatedUnitCost"), line.get("cost"), 0)
            component_lines.append({"lineKey": key, "lineName": name,
                                    "productId": _nc(line.get("productId")), "qtyPerUnit": qty,
                                    "linked": False, "pricedUnitCost": unit, "card": None,
                                    "supplySource": source})
            packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": unit})
            continue
        counts["linked"] += 1
        cards = _active_cards(product)
        card = cards[0] if cards else None
        if product and not _truthy(product.get("hsCode")):
            counts["hs"] += 1
        if card and not _truthy(card.get("incoterms")) and \
                not _truthy((card.get("supplier") or {}).get("incoterms")):
            counts["incoterms"] += 1
        if card and not _truthy(card.get("countryOfOrigin")):
            counts["origin"] += 1
        landed = landed_component_cost(_nc(card.get("unitCost") if card else None,
                                           line.get("cost"), 0),
                                       _nc(card.get("deliveryCost") if card else None, 0))
        component_lines.append({
            "lineKey": key, "lineName": name, "productId": _nc(line.get("productId")),
            "qtyPerUnit": qty, "linked": True, "pricedUnitCost": landed["landedUnitCost"],
            "supplySource": source,
            "card": {"unitCost": card.get("unitCost"), "deliveryCost": _nc(card.get("deliveryCost")),
                     "updatedAt": _nc(card.get("updatedAt")), "tiers": _tiers(card)}
            if card else None,
            "landed": landed})
        packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": landed["landedUnitCost"]})
    target = (fg or {}).get("targetQty")
    run = facts.get("quote_run") or {}
    run_qty = run.get("quantity")
    return {
        "project": project, "formulation": formulation, "formulationPin": resolved["pin"],
        "bom": bom, "bomLink": selected["link"],
        "unread": [name for name in QUOTE_RELATIONS if facts.get(name) is None],
        "rates": facts.get("rate_settings") or None,
        "resolutionSource": resolved["source"],
        "coreTargetPrice": core.get("target_price"),
        "specificGravity": specific_gravity, "fillWeightG": fill_weight_g,
        # G9: a customer-supplied line costs PLX nothing.
        "bomTotal": rollup_bom_total([
            {"quantity": line.get("quantity"), "cost": 0, "estimatedUnitCost": None}
            if _supply_for(supply["byBomLineId"], line.get("id")) != "PLX" else
            {"quantity": line.get("quantity"), "cost": line.get("cost"),
             "estimatedUnitCost": line.get("estimatedUnitCost")} for line in lines_in]),
        "rawMaterials": raw, "packaging": packaging, "componentLines": component_lines,
        "fgTargetQty": target if target is not None and _finite(target) and target > 0 else None,
        "runQty": run_qty if run_qty is not None and _finite(run_qty) and run_qty > 0 else None,
        "unsourcedLineCount": counts["unsourced"], "linkedLineCount": counts["linked"],
        "missingHsCodeCount": counts["hs"], "missingIncotermsCount": counts["incoterms"],
        "missingOriginCount": counts["origin"],
        "customerSuppliedLineCount": counts["customer"],
        "customerPendingLineCount": counts["pending"],
        "customerSupplied": customer_supplied, "supplyBasis": supply_basis}


def line_cost_evidence(product_ids: Sequence[Any], po_lines: Sequence[Mapping[str, Any]],
                       inventory: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``loadLineCostEvidence`` over supplied rows: the newest RECEIVED PO line per product
    (``receivedDate desc, updatedAt desc``) with header freight allocated by received value, and
    the weighted-average stock cost across the product's inventory rows."""
    ids = list(dict.fromkeys(i for i in product_ids if _truthy(i)))
    last_po: dict[str, Any] = {}
    stock: dict[str, Any] = {}
    if not ids:
        return {"lastPo": last_po, "stock": stock}
    received = [line for line in po_lines if line.get("productId") in ids
                and _is_num(line.get("quantityReceived")) and line["quantityReceived"] > 0
                and (line.get("purchaseOrder") or {}).get("receivedDate") is not None]
    received.sort(key=lambda line: _iso(line.get("updatedAt")) or "", reverse=True)
    received.sort(key=lambda line: _iso(line["purchaseOrder"]["receivedDate"]) or "",
                  reverse=True)
    newest: dict[str, Mapping[str, Any]] = {}
    for line in received:
        newest.setdefault(line["productId"], line)
    for product_id, line in newest.items():
        siblings = [{"id": x.get("id"), "unitPrice": x.get("unitPrice"),
                     "quantityReceived": x.get("quantityReceived")}
                    for x in po_lines if x.get("purchaseOrderId") == line.get("purchaseOrderId")]
        alloc = allocate_po_freight_per_unit(line["purchaseOrder"].get("shippingCost"),
                                             siblings or [line], line.get("id"))
        last_po[product_id] = {
            "source": "last_po_landed_approx", "poNumber": line["purchaseOrder"].get("poNumber"),
            "unitPrice": line.get("unitPrice"), "freightPerUnit": alloc["freightPerUnit"],
            "landedApproxUnitCost": _round(line["unitPrice"] + alloc["freightPerUnit"], 1e4)
            if alloc["freightPerUnit"] is not None else None,
            "quantityReceived": line["quantityReceived"], "freightReason": alloc["reason"],
            "asOf": _iso(line["purchaseOrder"]["receivedDate"]),
            "_source": [line.get("_source"), line["purchaseOrder"].get("_source")]}
    rows_by: dict[str, list[Mapping[str, Any]]] = {}
    for row in inventory:
        if row.get("productId") in ids and _truthy(row.get("productId")):
            rows_by.setdefault(row["productId"], []).append(row)
    for product_id, rows in rows_by.items():
        total_qty = costed_qty = costed_value = 0.0
        newest_ms: int | None = None
        for row in rows:
            qty = max(0.0, _nc(_to_num(row.get("currentQuantity")), 0.0))
            total_qty = total_qty + qty
            cost = _to_num(row.get("unitCost"))
            if cost is not None and qty > 0:
                costed_qty = costed_qty + qty
                costed_value = costed_value + qty * cost
            ms = _epoch_ms(_iso(_nc(row.get("lastTransactionAt"), row.get("updatedAt"))))
            if ms is not None and (newest_ms is None or ms > newest_ms):
                newest_ms = ms
        if costed_qty > 0:
            stock[product_id] = {"evidence": {
                "source": "stock_weighted_avg", "unitCost": _round(costed_value / costed_qty, 1e4),
                "quantityOnHand": total_qty,
                "asOf": _iso(_EPOCH + timedelta(milliseconds=newest_ms))
                if newest_ms is not None else None}, "missingReason": None,
                "_source": [row.get("_source") for row in rows]}
        else:
            stock[product_id] = {"evidence": None, "missingReason": "stock-cost-missing",
                                 "_source": [row.get("_source") for row in rows]}
    return {"lastPo": last_po, "stock": stock}


def build_line_cost_provenance(component_lines: Sequence[Mapping[str, Any]], qty_basis: Any,
                               evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``buildLineCostProvenance`` (evidence reads in the Workbench never fail silently: a
    caller with a failed read passes no rows and the Workbench layer names it)."""
    run_tier = pick_run_volume_tier(qty_basis)
    out = []
    for line in component_lines:
        if line.get("supplySource", "PLX") != "PLX":
            # G9: no card, PO or stock cost applies to what the customer sends.
            out.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                        "productId": line["productId"], "qtyPerUnit": line["qtyPerUnit"],
                        "pricedUnitCost": line["pricedUnitCost"], "cardTier": None,
                        "lastPo": None, "stock": None,
                        "missing": ["card:customer-supplied", "last_po:customer-supplied",
                                    "stock:customer-supplied"], "evidenceDbError": False})
            continue
        missing: list[str] = []
        card_tier = None
        if line["card"]:
            card_tier = pick_supplier_card_tier_price(
                line["card"], qty_basis * line["qtyPerUnit"]
                if qty_basis is not None and qty_basis > 0 else None, run_tier)
        else:
            missing.append("card:no-supplier-card" if line["linked"] else "card:line-unsourced")
        last_po = stock = None
        if not _truthy(line["productId"]):
            missing += ["last_po:no-product-link", "stock:no-product-link"]
        else:
            last_po = evidence["lastPo"].get(line["productId"])
            if not last_po:
                missing.append("last_po:none-received")
            entry = evidence["stock"].get(line["productId"])
            if not entry:
                missing.append("stock:no-inventory-row")
            elif entry["evidence"]:
                stock = entry["evidence"]
            else:
                missing.append(f"stock:{entry['missingReason'] or 'cost-missing'}")
        out.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                    "productId": line["productId"], "qtyPerUnit": line["qtyPerUnit"],
                    "pricedUnitCost": line["pricedUnitCost"], "cardTier": card_tier,
                    "lastPo": {k: v for k, v in last_po.items() if k != "_source"}
                    if last_po else None,
                    "stock": stock, "missing": missing, "evidenceDbError": False})
    return out


def _rates(rates: Mapping[str, Any] | None, key: str, default: float) -> Any:
    value = (rates or {}).get(key)
    return value if _is_num(value) else default


def _estimate(q: Mapping[str, Any], mass: float, packaging: Sequence[Mapping[str, Any]],
              asm_hours: Any, cmp_hours: Any, assembly_rate: Any, overhead: Mapping[str, Any],
              overhead_rate: Any, margin_pct: Any) -> dict[str, Any]:
    rates = q["rates"]
    return estimate_unit_cost({
        "fillWeightG": mass * 1000, "rawMaterials": q["rawMaterials"], "packaging": packaging,
        "rawMaterialsAdjustmentPct": _rates(rates, "rawMaterialsAdjustmentRate",
                                            RATE_DEFAULTS["rawMaterialsAdjustmentPct"]),
        "assemblyHoursPerUnit": _nc(asm_hours, 0), "compoundingHoursPerUnit": _nc(cmp_hours, 0),
        "assemblyRate": assembly_rate,
        "batchingRate": _rates(rates, "batchingQuotingLaborRate", RATE_DEFAULTS["batchingRate"]),
        "overheadRate": overhead_rate, "overheadHoursPerUnit": _nc(overhead["hoursPerUnit"], 0),
        "marginPct": margin_pct})


def _overhead(rates: Any, hours: Any, source: Any, override: Mapping[str, Any] | None) -> dict:
    override = override or {}
    return derive_uptime_overhead(overhead_rate_from_settings(rates), hours, source,
                                  override.get("expectedUnitsPerShift"),
                                  override.get("uptimeHours"))


def _asked(spec: Any) -> dict[str, Any] | None:
    """``normalizeAskedVolume``: a number, or an object's ``askedUnits``; finite and > 0."""
    obj = {"askedUnits": spec} if _is_num(spec) else {
        "askedUnits": spec.get("askedUnits") if isinstance(spec, Mapping) else None}
    if not _finite(obj["askedUnits"]) or obj["askedUnits"] <= 0:
        return None
    return obj


def _vessel_charges(plan: Any) -> int | None:
    """The charge count of a cited vessel plan (a positive whole number), None without one."""
    if plan is None:
        return None
    charges = plan.get("charges") if isinstance(plan, Mapping) else None
    if not (_is_num(charges) and charges >= 1 and float(charges).is_integer()):
        raise LandedCostError("vessel_plan_charges_must_be_a_positive_whole_number")
    return int(charges)


def portal_quote_landed(facts: Mapping[str, Any], *,
                        asked_volumes: Sequence[Any] | None = None) -> dict[str, Any]:
    """``quoteProjectLanded`` (persist=false) over the records it reads (``quote_facts``, plus
    ``fg_routings``, ``draft_cost_overrides``, ``setup_standards`` and the quote run quantity):
    the single estimate, the census, the per-line provenance and one matrix column per asked
    volume, as the Portal computes them, hours included. ``vessel_plans``
    (``{String(askedUnits): {"charges", "_source"}}``) is the one input not derived here: the
    compounding standard and vessel derivation are not ported; without a plan the Portal's own
    no-pick path (one charge) applies."""
    q = quote_facts(facts)
    rates = q["rates"]
    key = q["project"].get("projectKey")
    mass = fill_mass_kg(q["fillWeightG"], q["specificGravity"])
    assembly_rate = _rates(rates, "assemblyQuotingLaborRate", RATE_DEFAULTS["assemblyRate"])
    setup_rows = facts.get("setup_standards")
    routing, routing_fill = primary_routing(facts.get("fg_routings"), key, setup_rows)
    drafts = draft_cost_rows(facts.get("draft_cost_overrides"), key)
    setup_standard = resolve_setup_standard(setup_rows if setup_rows is not None
                                            else [WORKBOOK_SETUP_SEED])
    labour = resolve_quote_hours(key, drafts, routing, q["runQty"], q["fgTargetQty"],
                                 assembly_rate)["labour"]
    placeholder = labour["source"] == "missing"
    ladder = resolve_margin_ladder(rates)
    overhead = _overhead(rates, labour["assemblyHoursPerUnit"], labour["assemblySource"],
                         resolve_expected_units_override(drafts, key, labour["pricedTierCode"]))
    estimate = _estimate(q, mass, q["packaging"], labour["assemblyHoursPerUnit"],
                         labour["compoundingHoursPerUnit"], assembly_rate, overhead,
                         overhead["hourlyRate"], ladder["targetPct"])
    qty_basis = _nc(q["runQty"], q["fgTargetQty"])
    # G9: no purchasing or stock evidence for customer-supplied lines.
    evidence = line_cost_evidence([line["productId"] for line in q["componentLines"]
                                   if line["supplySource"] == "PLX"],
                                  facts.get("purchase_order_lines") or [],
                                  facts.get("inventory") or [])
    provenance = build_line_cost_provenance(q["componentLines"], qty_basis, evidence)
    provenance_lines = [{"lineName": line["lineName"],
                         "cardUnitCost": (prov["cardTier"] or {}).get("unitCost"),
                         "cardUpdatedAt": _iso((line["card"] or {}).get("updatedAt")),
                         "lastPoUnitPrice": (prov["lastPo"] or {}).get("unitPrice"),
                         "lastPoReceivedAt": (prov["lastPo"] or {}).get("asOf")}
                        for line, prov in zip(q["componentLines"], provenance)]
    stock_lines = [{"lineName": line["lineName"], "qtyPerUnit": line["qtyPerUnit"],
                    "pricedUnitCost": line["pricedUnitCost"],
                    "stockUnitCost": (prov["stock"] or {}).get("unitCost"),
                    "stockQuantityOnHand": (prov["stock"] or {}).get("quantityOnHand"),
                    "requiredQty": qty_basis * line["qtyPerUnit"]
                    if qty_basis is not None and qty_basis > 0 else None}
                   for line, prov in zip(q["componentLines"], provenance)]
    specs = [s for s in map(_asked, asked_volumes if asked_volumes is not None
                            else quote_suggestion_volumes(rates)) if s is not None]
    specs.sort(key=lambda s: s["askedUnits"])
    plans = facts.get("vessel_plans") or {}
    if not isinstance(plans, Mapping):
        raise LandedCostError("vessel_plans_must_map_asked_units_to_a_plan")
    columns = []
    for spec in specs:
        units = spec["askedUnits"]
        charges = _vessel_charges(plans.get(js_string(units)))
        hours = volume_column_labour(units, key, drafts, routing, setup_standard, charges,
                                     assembly_rate, rates)
        col_labour, col_overhead = hours["labour"], hours["overhead"]
        comp = []
        for line in q["componentLines"]:
            demand = units * line["qtyPerUnit"]
            tier_code = pick_run_volume_tier(demand)
            if line["supplySource"] != "PLX":
                # G9 (G-D4): $0 at every volume, whatever card PLX may hold.
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": 0,
                             "unitCostSource": "priced_basis", "supplySource": line["supplySource"],
                             "tierMinQty": None, "landed": None})
            elif line["card"]:
                tier = pick_supplier_card_tier_price(line["card"], demand, tier_code)
                landed = landed_component_cost(tier["unitCost"], _nc(line["card"]["deliveryCost"], 0))
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": landed["landedUnitCost"],
                             "unitCostSource": "card_tier_landed", "supplySource": "PLX",
                             "tierMinQty": tier["tierMinQty"], "landed": landed})
            else:
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": line["pricedUnitCost"],
                             "unitCostSource": "priced_basis", "supplySource": "PLX",
                             "tierMinQty": None, "landed": None})
        col_estimate = _estimate(q, mass, [{"name": c["lineName"], "qtyPerUnit": c["qtyPerUnit"],
                                            "unitCost": c["unitCost"]} for c in comp],
                                 col_labour["assemblyHoursPerUnit"],
                                 col_labour["compoundingHoursPerUnit"], assembly_rate,
                                 col_overhead, col_overhead["hourlyRate"], ladder["targetPct"])
        columns.append({"askedUnits": units, "labour": col_labour, "vesselCharges": charges,
                        "componentLines": comp,
                        "lines": col_estimate["lines"], "estimate": col_estimate,
                        "unitCost": col_estimate["totalCostPerUnit"],
                        "marginTiers": price_margin_tiers(col_estimate["totalCostPerUnit"], ladder),
                        "overhead": col_overhead})
        if q["customerSupplied"]:
            columns[-1]["customerSupplied"] = customer_supplied_requirements(
                q["customerSupplied"], units)
    census = derive_ready_to_quote({
        "fillWeightG": q["fillWeightG"], "specificGravity": q["specificGravity"],
        "specificGravityConfirmed": q["specificGravity"] is not None and q["specificGravity"] > 0,
        "unsourcedLineCount": q["unsourcedLineCount"], "linkedLineCount": q["linkedLineCount"],
        "missingHsCodeCount": q["missingHsCodeCount"],
        "missingIncotermsCount": q["missingIncotermsCount"],
        "missingOriginCount": q["missingOriginCount"],
        "customerSuppliedLineCount": q["customerSuppliedLineCount"],
        "customerPendingLineCount": q["customerPendingLineCount"],
        "customerSuppliedRawCount": sum(m["kind"] == "raw" for m in q["customerSupplied"]),
        "labourIsPlaceholder": placeholder,
        "sellPricePerUnit": estimate["sellPricePerUnit"],
        "retailPerUnit": parse_money(q["coreTargetPrice"]),
        "provenanceLines": provenance_lines, "stockFillLines": stock_lines,
        "pricedTotalCostPerUnit": estimate["totalCostPerUnit"]})
    result = {"estimate": estimate, "fillWeightG": q["fillWeightG"],
              "specificGravity": q["specificGravity"], "fillMassKg": mass,
              "bomTotal": q["bomTotal"], "resolutionSource": q["resolutionSource"],
              "labourIsPlaceholder": placeholder, "labour": labour, "marginLadder": ladder,
              "marginTiers": price_margin_tiers(estimate["totalCostPerUnit"], ladder),
              "overhead": overhead, "census": census, "lineProvenance": provenance,
              "volumeMatrix": columns, "_facts": q, "_evidence": evidence,
              "_labour": {"routing": routing, "routing_fill": routing_fill, "drafts": drafts,
                          "setup_standard": setup_standard}}
    if q["customerSupplied"]:
        # G9: what the customer must send at the priced qty basis (run qty, else FG target).
        result["customerSupplied"] = customer_supplied_requirements(q["customerSupplied"],
                                                                    qty_basis)
    return result
