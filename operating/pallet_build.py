"""Pallet builder and skid math (LOG-01, LOG-08).

The Portal's pallet builder and 3PL skid math, ported line by line from
petralabx/plx-customer-portal staging @ 48d76e8 (pinned in ``verify/upstream-pins.json``):

* ``portal/src/lib/wms/pallet-layout.ts``: interlacing patterns and the Layer A/B placement
  (``layout_layer_pair``; only what ``buildPallets`` uses: the UI helpers ``layoutLayer``,
  ``compareLayerPatterns`` and ``densityWarningVsBlock`` are not ported);
* ``portal/src/lib/wms/pallet-build.ts``: ``build_pallets`` (cases per layer and pallet, full
  and partial pallets, handling units with density, DIM weight and NMFC freight class);
* ``portal/src/lib/3pl/skid-math.ts``: ``compute_skids`` and its helpers.

Arithmetic is IEEE double precision exactly as the Portal computes it (``Math.round`` ties
toward +infinity, ``Math.floor`` / ``Math.ceil``, ``a || b`` / ``a ?? b`` defaults), so the
golden test (``operating/tests/test_portal_landed_engine.py``) compares the Portal TypeScript's own
outputs with these value for value. Result keys keep the Portal's names.

The Portal reads the master-case dimensions and gross weight from ``app_product_pack_spec``,
which the Workbench reporting API does not serve: nothing here is called with a guessed
dimension, and a missing one stays in ``missing`` exactly as the Portal reports it.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

PORTAL_REVISION = "48d76e835660505091f764636ec0db0d38455f37"

# ---- pallet-layout.ts --------------------------------------------------------------------

INTERLACE_PATTERNS = ("block", "row", "brick", "pinwheel", "split_row", "hybrid_pinwheel")
_LEGACY = {"column": "block", "brick": "brick", "pinwheel": "pinwheel"}


def normalize_pattern(raw: Any = None) -> str:
    if not raw:
        return "block"
    if raw in _LEGACY:
        return _LEGACY[raw]
    if raw in INTERLACE_PATTERNS:
        return raw
    return "block"


def _place(x: float, y: float, w: float, h: float, rotated: bool) -> dict[str, Any]:
    return {"x": x, "y": y, "w": w, "h": h, "rotated": rotated}


def _effective_base(base_l: float, base_w: float, overhang_pct: float) -> tuple[float, float]:
    grow = 1 + max(0, overhang_pct) / 100
    return base_l * grow, base_w * grow


def _span_of(placements: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    max_x = max_y = 0
    for p in placements:
        max_x = max(max_x, p["x"] + p["w"])
        max_y = max(max_y, p["y"] + p["h"])
    return max_x, max_y


def _center_on_deck(placements: list[dict[str, Any]], eff_l: float,
                    eff_w: float) -> list[dict[str, Any]]:
    if not placements:
        return placements
    used_l, used_w = _span_of(placements)
    dx = max(0, (eff_l - used_l) / 2)
    dy = max(0, (eff_w - used_w) / 2)
    if dx == 0 and dy == 0:
        return placements
    return [dict(p, x=p["x"] + dx, y=p["y"] + dy) for p in placements]


def _rotate180(placements: Sequence[Mapping[str, Any]], eff_l: float,
               eff_w: float) -> list[dict[str, Any]]:
    return [dict(p, x=eff_l - p["x"] - p["w"], y=eff_w - p["y"] - p["h"]) for p in placements]


def _overlaps(a: Mapping[str, Any], b: Mapping[str, Any], eps: float = 1e-6) -> bool:
    return not (a["x"] + a["w"] <= b["x"] + eps or b["x"] + b["w"] <= a["x"] + eps or
                a["y"] + a["h"] <= b["y"] + eps or b["y"] + b["h"] <= a["y"] + eps)


def _fits(p: Mapping[str, Any], eff_l: float, eff_w: float, eps: float = 1e-6) -> bool:
    return p["x"] >= -eps and p["y"] >= -eps and p["x"] + p["w"] <= eff_l + eps and \
        p["y"] + p["h"] <= eff_w + eps


def _can_place(candidate: Mapping[str, Any], existing: Sequence[Mapping[str, Any]],
               eff_l: float, eff_w: float) -> bool:
    if not _fits(candidate, eff_l, eff_w):
        return False
    return not any(_overlaps(candidate, e) for e in existing)


def _grid(case_l: float, case_w: float, eff_l: float, eff_w: float,
          rotated: bool) -> list[dict[str, Any]]:
    w = case_w if rotated else case_l
    h = case_l if rotated else case_w
    cols = math.floor(eff_l / w + 1e-9)
    rows = math.floor(eff_w / h + 1e-9)
    if cols <= 0 or rows <= 0:
        return []
    return [_place(c * w, r * h, w, h, rotated) for r in range(rows) for c in range(cols)]


def _best_grid(case_l: float, case_w: float, eff_l: float, eff_w: float,
               allow_rotate: bool) -> tuple[list[dict[str, Any]], bool]:
    a = _grid(case_l, case_w, eff_l, eff_w, False)
    if not allow_rotate:
        return a, False
    b = _grid(case_l, case_w, eff_l, eff_w, True)
    if len(b) > len(a):
        return b, True
    return a, False


def _brick_bond(case_l: float, case_w: float, eff_l: float, eff_w: float, rotated: bool,
                invert: bool) -> list[dict[str, Any]]:
    w = case_w if rotated else case_l
    h = case_l if rotated else case_w
    rows = math.floor(eff_w / h + 1e-9)
    if rows <= 0 or w <= 0:
        return []
    half = w / 2
    out = []
    for r in range(rows):
        offset_row = r % 2 == 0 if invert else r % 2 == 1
        offset = half if offset_row else 0
        cols = math.floor((eff_l - offset) / w + 1e-9)
        out.extend(_place(offset + c * w, r * h, w, h, rotated) for c in range(cols))
    return out


def _yi_windmill_unit(case_l: float, case_w: float, ox: float = 0,
                      oy: float = 0) -> list[dict[str, Any]]:
    big_l, big_w = case_l, case_w
    return [_place(ox + 0, oy + 0, big_l, big_w, False),
            _place(ox + big_l, oy + 0, big_w, big_l, True),
            _place(ox + 0, oy + big_w, big_w, big_l, True),
            _place(ox + big_w, oy + big_l, big_l, big_w, False)]


def _yi_windmill_unit_mirror(case_l: float, case_w: float, ox: float = 0,
                             oy: float = 0) -> list[dict[str, Any]]:
    big_l, big_w = case_l, case_w
    tile = big_l + big_w
    return [_place(ox + big_w, oy + 0, big_l, big_w, False),
            _place(ox + 0, oy + 0, big_w, big_l, True),
            _place(ox + tile - big_w, oy + big_w, big_w, big_l, True),
            _place(ox + 0, oy + big_l, big_l, big_w, False)]


def _greedy_fill(case_l: float, case_w: float, eff_l: float, eff_w: float,
                 existing: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(existing)
    orientations = ((case_l, case_w, False), (case_w, case_l, True))
    step = min(case_l, case_w) / 2
    for _ in range(2):  # two passes are enough for pallet-scale remainders
        for ow, oh, rotated in orientations:
            y = 0
            while y + oh <= eff_w + 1e-9:
                x = 0
                while x + ow <= eff_l + 1e-9:
                    p = _place(x, y, ow, oh, rotated)
                    if _can_place(p, out, eff_l, eff_w):
                        out.append(p)
                    x += step
                y += step
    return out


def _pinwheel_arms(case_l: float, case_w: float, eff_l: float, eff_w: float,
                   fill_remainders: bool = True) -> list[dict[str, Any]]:
    tile = case_l + case_w
    if tile <= 0:
        return _best_grid(case_l, case_w, eff_l, eff_w, True)[0]

    def try_tiling(mirror: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cols = math.floor(eff_l / tile + 1e-9)
        rows = math.floor(eff_w / tile + 1e-9)
        for r in range(rows):
            for c in range(cols):
                unit = (_yi_windmill_unit_mirror if mirror else _yi_windmill_unit)(
                    case_l, case_w, c * tile, r * tile)
                for p in unit:
                    if _can_place(p, out, eff_l, eff_w):
                        out.append(p)
        return _greedy_fill(case_l, case_w, eff_l, eff_w, out) if fill_remainders else out

    a = try_tiling(False)
    b = try_tiling(True)
    best = b if len(b) > len(a) else a
    if len(best) < 4:
        best = _best_grid(case_l, case_w, eff_l, eff_w, True)[0]
    return best


def _split_row(case_l: float, case_w: float, eff_l: float, eff_w: float,
               allow_rotate: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bottom_h = case_w
    top_start = bottom_h
    top_h = eff_w - top_start
    bot_cols = math.floor(eff_l / case_l + 1e-9)
    bot_rows = math.floor(bottom_h / case_w + 1e-9)
    for r in range(bot_rows):
        for c in range(bot_cols):
            out.append(_place(c * case_l, r * case_w, case_l, case_w, False))
    if allow_rotate and top_h >= case_l - 1e-6:
        top_cols = math.floor(eff_l / case_w + 1e-9)
        top_rows = math.floor(top_h / case_l + 1e-9)
        for r in range(top_rows):
            for c in range(top_cols):
                p = _place(c * case_w, top_start + r * case_l, case_w, case_l, True)
                if _can_place(p, out, eff_l, eff_w):
                    out.append(p)
    else:
        for p in _grid(case_l, case_w, eff_l, top_h, False):
            q = dict(p, y=p["y"] + top_start)
            if _can_place(q, out, eff_l, eff_w):
                out.append(q)
    if not out:
        return _best_grid(case_l, case_w, eff_l, eff_w, allow_rotate)[0]
    return out


def _layer(pattern: str, key: str, case_l: float, case_w: float, base_l: float,
           base_w: float, overhang_pct: float, eff_l: float, eff_w: float,
           placements: list[dict[str, Any]]) -> dict[str, Any]:
    centered = _center_on_deck(placements, eff_l, eff_w)
    return {"pattern": pattern, "layerKey": key, "caseLengthIn": case_l, "caseWidthIn": case_w,
            "baseLengthIn": base_l, "baseWidthIn": base_w, "effectiveLengthIn": eff_l,
            "effectiveWidthIn": eff_w, "overhangPct": overhang_pct, "count": len(centered),
            "placements": centered, "usedRotated": any(p["rotated"] for p in centered)}


def layout_layer_pair(case_length_in: float, case_width_in: float, base_length_in: float,
                      base_width_in: float, *, overhang_pct: float | None = None,
                      pattern: Any = None, allow_rotate: bool | None = None) -> dict[str, Any]:
    """``layoutLayerPair``: Layer A and B placements; quoting density min(A, B)."""
    pattern = normalize_pattern(pattern)
    overhang_pct = 0 if overhang_pct is None else overhang_pct
    allow_rotate = True if allow_rotate is None else allow_rotate
    cl, cw = case_length_in, case_width_in
    eff_l, eff_w = _effective_base(base_length_in, base_width_in, overhang_pct)
    a: list[dict[str, Any]] = []
    b: list[dict[str, Any]] = []
    if pattern == "block":
        a, _ = _best_grid(cl, cw, eff_l, eff_w, allow_rotate)
        b = a
    elif pattern == "row":
        a, used_rotated = _best_grid(cl, cw, eff_l, eff_w, allow_rotate)
        alt = _grid(cl, cw, eff_l, eff_w, not used_rotated) if allow_rotate else a
        b = alt if len(alt) >= math.floor(len(a) * 0.75) else a
    elif pattern == "brick":
        g, used_rotated = _best_grid(cl, cw, eff_l, eff_w, allow_rotate)
        a = _brick_bond(cl, cw, eff_l, eff_w, used_rotated, False)
        b = _brick_bond(cl, cw, eff_l, eff_w, used_rotated, True)
        if len(a) < math.floor(len(g) * 0.7):  # a poor bond falls back to the grid, A = B
            a = b = g
    elif pattern in ("pinwheel", "hybrid_pinwheel"):  # hybrid: the same engine, a UX choice
        a = _pinwheel_arms(cl, cw, eff_l, eff_w, True)
        b = _rotate180(a, eff_l, eff_w)
    elif pattern == "split_row":
        a = _split_row(cl, cw, eff_l, eff_w, allow_rotate)
        b = _rotate180(a, eff_l, eff_w)
    layer_a = _layer(pattern, "A", cl, cw, base_length_in, base_width_in, overhang_pct, eff_l,
                     eff_w, a)
    layer_b = _layer(pattern, "B", cl, cw, base_length_in, base_width_in, overhang_pct, eff_l,
                     eff_w, b)
    return {"pattern": pattern, "overhangPct": overhang_pct,
            "casesPerLayer": min(layer_a["count"], layer_b["count"]) or
            max(layer_a["count"], layer_b["count"]),
            "layerA": layer_a, "layerB": layer_b}


# ---- pallet-build.ts ---------------------------------------------------------------------

DEFAULT_PALLET_LENGTH_IN = 48
DEFAULT_PALLET_WIDTH_IN = 40
DEFAULT_PALLET_TARE_LB = 40
DEFAULT_MAX_STACK_HEIGHT_IN = 60  # case stack above the deck when the spec does not cap it
PALLET_DECK_HEIGHT_IN = 6
PARCEL_DIM_DIVISOR = 139  # domestic parcel DIM divisor (in3/lb)


def _js_round(value: float, dp: int = 2) -> float:
    """``round(n, dp)``: ``Math.round(n * 10 ** dp) / 10 ** dp`` (ties toward +infinity)."""
    f = 10 ** dp
    scaled = value * f
    if not math.isfinite(scaled):
        return scaled
    floor = math.floor(scaled)
    return float(floor + 1 if scaled - floor >= 0.5 else floor) / f


def _truthy(value: Any) -> bool:
    return not (value is None or value is False or value == 0 or value == "" or
                (isinstance(value, float) and math.isnan(value)))


def _is_number(value: Any) -> bool:
    """``typeof value === "number"``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nc(value: Any, default: Any) -> Any:
    """``value ?? default``."""
    return default if value is None else value


def freight_class_from_density(pcf: float) -> float:
    """NMFC density (lb/ft3) to freight class, the standard 18-step LTL scale."""
    if not math.isfinite(pcf) or pcf <= 0:
        return 500
    for floor, cls in ((50, 50), (35, 55), (30, 60), (22.5, 65), (15, 70), (13.5, 77.5),
                       (12, 85), (10.5, 92.5), (9, 100), (8, 110), (7, 125), (6, 150),
                       (5, 175), (4, 200), (3, 250), (2, 300), (1, 400)):
        if pcf >= floor:
            return cls
    return 500


def dim_weight(length_in: float, width_in: float, height_in: float,
               divisor: float = PARCEL_DIM_DIVISOR) -> int:
    return math.ceil((length_in * width_in * height_in) / divisor)


def density_pcf(weight_lb: float, length_in: float, width_in: float, height_in: float) -> float:
    cubic_ft = (length_in * width_in * height_in) / 1728
    return weight_lb / cubic_ft if cubic_ft > 0 else 0


def cases_per_layer(case_length_in: float, case_width_in: float, base_length_in: float,
                    base_width_in: float, allow_rotate: bool = True,
                    overhang_pct: float = 0) -> int:
    """Block-stack heuristic: the better of both orientations; overhang grows the base."""
    grow = 1 + max(0, overhang_pct) / 100
    bl, bw = base_length_in * grow, base_width_in * grow

    def fit(cl: float, cw: float) -> int:
        return math.floor(bl / cl) * math.floor(bw / cw)
    straight = fit(case_length_in, case_width_in)
    rotated = fit(case_width_in, case_length_in) if allow_rotate else 0
    return max(straight, rotated)


def _carton_unit(quantity: float, l: float, w: float, h: float, wt: float, stackable: bool,
                 label: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    density = density_pcf(wt, l, w, h)
    dw = dim_weight(l, w, h)
    return {"type": "carton", "quantity": quantity, "lengthIn": _js_round(l),
            "widthIn": _js_round(w), "heightIn": _js_round(h), "weightLb": _js_round(wt),
            "casesPerUnit": 1, "stackable": stackable, "description": label,
            "densityPcf": _js_round(density), "dimWeightLb": dw,
            "billableWeightLb": _js_round(max(wt, dw)),
            "freightClass": _nc(spec.get("freightClass"), freight_class_from_density(density)),
            "nmfcCode": _nc(spec.get("nmfcCode"), None), "hazmat": _nc(spec.get("hazmat"), False)}


def _pallet_unit(quantity: int, cases_on_pallet: int, layers: int, per_layer: int,
                 case_l: float, case_w: float, case_h: float, case_wt: float, base_l: float,
                 base_w: float, tare: float, stackable: bool, label: str,
                 spec: Mapping[str, Any], pattern: str = "block") -> dict[str, Any]:
    unit_height = layers * case_h + PALLET_DECK_HEIGHT_IN
    unit_weight = cases_on_pallet * case_wt + tare
    density = density_pcf(unit_weight, base_l, base_w, unit_height)
    tag = "" if pattern == "block" else f", {pattern}"
    return {"type": "pallet", "quantity": quantity, "lengthIn": _js_round(base_l),
            "widthIn": _js_round(base_w), "heightIn": _js_round(unit_height),
            "weightLb": _js_round(unit_weight), "casesPerUnit": cases_on_pallet,
            "stackable": stackable,
            "description": f"{label} — {cases_on_pallet} cs ({per_layer}/layer × "
                           f"{layers}{tag})",
            "densityPcf": _js_round(density), "dimWeightLb": None,
            "billableWeightLb": _js_round(unit_weight),
            "freightClass": _nc(spec.get("freightClass"), freight_class_from_density(density)),
            "nmfcCode": _nc(spec.get("nmfcCode"), None), "hazmat": _nc(spec.get("hazmat"), False)}


def _finalize(units: list[dict[str, Any]], meta: Mapping[str, Any]) -> dict[str, Any]:
    weight = billable = cubic = 0
    for hu in units:
        weight += hu["weightLb"] * hu["quantity"]
        billable += hu["billableWeightLb"] * hu["quantity"]
        cubic += ((hu["lengthIn"] * hu["widthIn"] * hu["heightIn"]) / 1728) * hu["quantity"]
    return {"ok": len(units) > 0, "handlingUnits": units, "totalWeightLb": _js_round(weight),
            "totalBillableWeightLb": _js_round(billable), "totalCubicFt": _js_round(cubic),
            **meta}


def build_pallets(spec: Mapping[str, Any], *, quantity_cases: float | None = None,
                  quantity_units: float | None = None, mode: str = "ltl",
                  description: str | None = None, overhang_pct: float | None = None,
                  max_layers: float | None = None, pattern: Any = None) -> dict[str, Any]:
    """``buildPallets``: ``spec`` is a ``PackSpecInput`` (Portal key names: caseLengthIn,
    caseWidthIn, caseHeightIn, caseGrossWeightLb, unitsPerCase, maxStackHeightIn, ...)."""
    missing: list[str] = []
    case_l, case_w = _nc(spec.get("caseLengthIn"), None), _nc(spec.get("caseWidthIn"), None)
    case_h, case_wt = _nc(spec.get("caseHeightIn"), None), _nc(spec.get("caseGrossWeightLb"), None)
    for key, value in (("caseLengthIn", case_l), ("caseWidthIn", case_w),
                       ("caseHeightIn", case_h), ("caseGrossWeightLb", case_wt)):
        if not _truthy(value) or value <= 0:
            missing.append(key)
    units_per_case = _nc(spec.get("unitsPerCase"), None)
    if _is_number(quantity_cases):
        total_cases = max(0, math.ceil(quantity_cases))
    elif _is_number(quantity_units):
        if not _truthy(units_per_case) or units_per_case <= 0:
            missing.append("unitsPerCase")
            total_cases = 0
        else:
            total_cases = math.ceil(quantity_units / units_per_case)
    else:
        missing.append("quantity")
        total_cases = 0
    label = description or spec.get("commodityDescription") or "Product"
    pattern = normalize_pattern(pattern)
    overhang = max(0, overhang_pct) if _is_number(overhang_pct) and math.isfinite(overhang_pct) \
        else _nc(spec.get("maxOverhangPct"), 0)
    if missing or total_cases <= 0:
        return {"ok": False, "missing": missing, "mode": mode, "totalCases": total_cases,
                "casesPerLayer": 0, "layersPerPallet": 0, "casesPerPallet": 0,
                "fullPallets": 0, "partialPalletCases": 0, "totalPallets": 0,
                "totalWeightLb": 0, "totalBillableWeightLb": 0, "totalCubicFt": 0,
                "handlingUnits": [], "overhangPct": overhang, "pattern": pattern,
                "layerLayout": None, "layerPair": None}
    base_l = spec.get("palletBaseLengthIn") or DEFAULT_PALLET_LENGTH_IN
    base_w = spec.get("palletBaseWidthIn") or DEFAULT_PALLET_WIDTH_IN
    tare = _nc(spec.get("palletTareWeightLb"), DEFAULT_PALLET_TARE_LB)
    allow_rotate = _nc(spec.get("allowRotate"), True)
    stackable = _nc(spec.get("stackable"), True)
    stack_h = spec.get("maxStackHeightIn")
    max_stack_h = stack_h if _truthy(stack_h) and stack_h > 0 else DEFAULT_MAX_STACK_HEIGHT_IN
    stack_w = spec.get("maxStackWeightLb")
    max_stack_w = stack_w if _truthy(stack_w) and stack_w > 0 else math.inf
    pair = layout_layer_pair(case_l, case_w, base_l, base_w, overhang_pct=overhang,
                             pattern=pattern, allow_rotate=allow_rotate)
    per_layer = pair["casesPerLayer"] if pair["casesPerLayer"] > 0 else \
        cases_per_layer(case_l, case_w, base_l, base_w, allow_rotate, overhang)
    loose = {"layersPerPallet": 0, "casesPerPallet": 0, "fullPallets": 0,
             "partialPalletCases": total_cases, "totalPallets": 0, "mode": mode,
             "missing": missing, "overhangPct": overhang, "pattern": pattern,
             "layerLayout": None, "layerPair": None}
    if mode == "parcel":  # rate individual master cartons, no palletizing
        return _finalize([_carton_unit(total_cases, case_l, case_w, case_h, case_wt, stackable,
                                       label, spec)],
                         {"totalCases": total_cases, "casesPerLayer": per_layer, **loose})
    if per_layer <= 0:
        return _finalize([_carton_unit(total_cases, case_l, case_w, case_h, case_wt, stackable,
                                       label, spec)],
                         {"totalCases": total_cases, "casesPerLayer": 0, **loose})
    height_layers = max(1, math.floor(max_stack_h / case_h))
    weight_layers = max(1, math.floor(max_stack_w / (per_layer * case_wt))) \
        if math.isfinite(max_stack_w) else math.inf
    layers = min(height_layers, weight_layers) if math.isfinite(weight_layers) else height_layers
    if _is_number(max_layers) and max_layers > 0:
        layers = min(layers, math.floor(max_layers))
    layers = max(1, layers)
    per_pallet = per_layer * layers
    full = math.floor(total_cases / per_pallet)
    partial = total_cases - full * per_pallet
    units = []
    if full > 0:
        units.append(_pallet_unit(full, per_pallet, layers, per_layer, case_l, case_w, case_h,
                                  case_wt, base_l, base_w, tare, stackable, label, spec, pattern))
    if partial > 0:
        units.append(_pallet_unit(1, partial, math.ceil(partial / per_layer), per_layer, case_l,
                                  case_w, case_h, case_wt, base_l, base_w, tare, stackable, label,
                                  spec, pattern))
    return _finalize(units, {
        "totalCases": total_cases, "casesPerLayer": per_layer, "layersPerPallet": layers,
        "casesPerPallet": per_pallet, "fullPallets": full, "partialPalletCases": partial,
        "totalPallets": full + (1 if partial > 0 else 0), "mode": mode, "missing": missing,
        "overhangPct": overhang, "pattern": pattern, "layerLayout": pair["layerA"],
        "layerPair": pair})


# ---- skid-math.ts --------------------------------------------------------------------------

def case_volume_cu_ft(spec: Mapping[str, Any]) -> float | None:
    """Cubic feet of a master case (L x W x H inches / 1728)."""
    dims = [spec.get(k) for k in ("caseLengthIn", "caseWidthIn", "caseHeightIn")]
    if any(d is None or d <= 0 for d in dims):
        return None
    return (dims[0] * dims[1] * dims[2]) / 1728


def _ti_hi(spec: Mapping[str, Any]) -> tuple[Any, Any, bool]:
    ti, hi = spec.get("fmCasesPerLayer"), spec.get("fmLayersPerPallet")
    return ti, hi, ti is not None and hi is not None and ti > 0 and hi > 0


def cases_per_skid_from_spec(spec: Mapping[str, Any]) -> float | None:
    """FM ti x hi when both are present, else ``build_pallets``' cases per pallet."""
    ti, hi, both = _ti_hi(spec)
    if both:
        return ti * hi
    if any(spec.get(k) is None for k in ("caseLengthIn", "caseWidthIn", "caseHeightIn",
                                         "caseGrossWeightLb")):
        return None
    try:
        cps = build_pallets(spec, quantity_cases=1_000_000)["casesPerPallet"]
    except Exception:  # noqa: BLE001 - the Portal's catch: no skid count
        return None
    return cps if cps > 0 else None


def skids_from_cases(cases: float, cases_per_skid: float) -> float:
    if cases_per_skid <= 0:
        return 0
    return cases / cases_per_skid


def compute_skids(cases: float, spec: Mapping[str, Any] | None = None,
                  explicit_skids: float | None = None) -> dict[str, Any]:
    if explicit_skids is not None and _is_number(explicit_skids) and \
            math.isfinite(explicit_skids):
        vol = case_volume_cu_ft(spec) if spec else None
        return {"casesPerSkid": 0, "skids": explicit_skids,
                "volumeCuFt": vol * cases if vol is not None else 0, "source": "explicit"}
    if not spec:
        return {"casesPerSkid": 0, "skids": 0, "volumeCuFt": 0, "source": "pallet_build"}
    _, _, both = _ti_hi(spec)
    cps = cases_per_skid_from_spec(spec)
    volume = _nc(case_volume_cu_ft(spec), 0) * cases
    if cps is None or cps <= 0:
        return {"casesPerSkid": 0, "skids": 0, "volumeCuFt": volume, "source": "pallet_build"}
    return {"casesPerSkid": cps, "skids": skids_from_cases(cases, cps), "volumeCuFt": volume,
            "source": "fm_ti_hi" if both else "pallet_build"}


def pack_spec_dims_complete(spec: Mapping[str, Any] | None) -> bool:
    if not spec:
        return False
    dims = [spec.get(k) for k in ("caseLengthIn", "caseWidthIn", "caseHeightIn")]
    _, _, both = _ti_hi(spec)
    return all(d is not None and d > 0 for d in dims) or both

