"""Live check of the money and unit rules (ontology.money_units): counts and enums only.

    python3 -m scripts.money_units_probe --session-projection SESSION_DIR \\
        [--previous-report DAILY.json]

Offline and with no client: opens, read-only, the projection generation this verifier
session committed (``scripts.ontology_projection_probe.open_session``: the session
producer ``ontology.session_projection`` built it over the live Portal mirror and BC
capture) and reads its build report. Nothing is projected, synced or captured here.

Recorded:

* ``gap_codes``: the money and unit gap codes of the generation (after the rules);
* ``gap_codes_by_source``: each of them by source family of the binding it was reported
  on (``bc``, ``portal_app``, ``fm``, ``sage``, ``silver``, ``gen``, ``other``;
  ``cross_record`` for a conflict, reported per type and property);
* ``rules``: the values each ``per_unit_rules`` rule and each currency rule decided, per
  binding and rule id (each fires only on a value that gap held on the base projection:
  three letters in any case were a code before and are never counted); ``undecided``: per binding, why no price-denominator rule did
  (``unit_blank``, ``units_disagree``, ``no_rule_holds``); ``currency_shapes``: the masked
  shapes of currency cells still invalid (letters ``A``, digits ``9``; never the text);
* ``without_rules``: what ``price_denominator_unknown`` and ``currency_invalid`` would be
  on the same generation with no rule (the gap plus the values rules decided: a rule
  fires only on a value that was that gap before), and ``reduction`` (share decided);
  ``price_denominator_halved`` / ``currency_invalid_halved``: at least half decided;
* ``ruled_price_conflicts``: property conflicts on a price whose denominator a rule
  decided (never compared before, so each would be a new ``conflict_across_records``),
  and ``ruled_currency_conflicts``: those on an amount whose currency a rule read (an
  invalid code before); ``no_new_conflicts`` is true when there are none of either;
* ``previous``: the money gap codes the previous daily's ``ontology.session_projection``
  recorded (``--previous-report``), and ``ratio_to_previous`` per code: context across
  days, never asserted (the data moves between days).

No generation of this session: ``error`` ``session_projection_unavailable`` (a fail,
never a pass). With no source in the generation it prints ``sources_blocked``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

PROBE = "money_units"
CHECK = "ontology.session_projection"

MONEY_GAPS = ("price_denominator_unknown", "per_unit_unresolved", "unit_unresolved",
              "currency_invalid", "currency_unresolved", "conflict_across_records",
              "conflict_not_compared_tax_basis", "conflict_not_compared_price_denominator",
              "conflict_not_compared_currency")
FAMILIES = (("bc.", "bc"), ("portal.app_", "portal_app"), ("portal.fm_", "fm"),
            ("portal.sage_", "sage"), ("portal.silver_", "silver"), ("gen.", "gen"))
PRICE_RULE = "money_rule.price_denominator."
CURRENCY_RULE = "money_rule.currency."
CONFLICTS = PRICE_RULE + "conflicts."
CURRENCY_CONFLICTS = CURRENCY_RULE + "conflicts."
UNDECIDED = ".undecided."
SHAPE = "currency_shape."


def family(where: str) -> str:
    """The source family of a gap's ``where`` (``Type.prop@binding``)."""
    if "@" not in where:
        return "cross_record"
    binding = where.split("@", 1)[1]
    return next((name for prefix, name in FAMILIES if binding.startswith(prefix)), "other")


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def summarize(report: Mapping[str, Any], previous: Mapping[str, Any] | None = None
              ) -> dict[str, Any]:
    """The check's fields over one build report (``{"counts", "gaps"}``)."""
    counts = report.get("counts") or {}
    gap_codes = {code: 0 for code in MONEY_GAPS}
    by_source: dict[str, dict[str, int]] = {code: {} for code in MONEY_GAPS}
    for gap in report.get("gaps") or []:
        code = gap.get("code")
        if code in gap_codes:
            gap_codes[code] += gap["count"]
            fam = family(str(gap.get("where", "")))
            by_source[code][fam] = by_source[code].get(fam, 0) + gap["count"]
    rules: dict[str, dict[str, int]] = {"price_denominator": {}, "currency": {}}
    undecided: dict[str, int] = {}
    shapes: dict[str, int] = {}
    conflicts: dict[str, int] = {}
    currency_conflicts: dict[str, int] = {}
    for name, n in sorted(counts.items()):
        if name.startswith(CONFLICTS):
            conflicts[name[len(CONFLICTS):]] = n
        elif name.startswith(CURRENCY_CONFLICTS):
            currency_conflicts[name[len(CURRENCY_CONFLICTS):]] = n
        elif name.startswith(PRICE_RULE) and UNDECIDED in name:
            undecided[name[len(PRICE_RULE):]] = n
        elif name.startswith(PRICE_RULE):
            rules["price_denominator"][name[len(PRICE_RULE):]] = n
        elif name.startswith(CURRENCY_RULE):
            rules["currency"][name[len(CURRENCY_RULE):]] = n
        elif name.startswith(SHAPE):
            shapes[name[len(SHAPE):]] = n
    decided = {"price_denominator_unknown": sum(rules["price_denominator"].values()),
               "currency_invalid": sum(rules["currency"].values())}
    without = {code: gap_codes[code] + n for code, n in decided.items()}
    reduction = {code: _ratio(decided[code], without[code]) for code in decided}
    halved = {code: without[code] > 0 and 2 * gap_codes[code] <= without[code]
              for code in decided}
    before = {code: (previous or {}).get(code) for code in MONEY_GAPS}
    recorded = {code: n for code, n in before.items() if isinstance(n, int)}
    ruled = sum(conflicts.values())
    ruled_currency = sum(currency_conflicts.values())
    return {
        "probe": PROBE,
        "gap_codes": gap_codes,
        "gap_codes_by_source": {code: dict(sorted(v.items())) for code, v in by_source.items()},
        "rules": rules, "undecided": undecided, "currency_shapes": shapes,
        "without_rules": without, "reduction": reduction,
        "price_denominator_halved": halved["price_denominator_unknown"],
        "currency_invalid_halved": halved["currency_invalid"],
        "ruled_price_conflicts": ruled, "ruled_price_conflicts_by_property": conflicts,
        "ruled_currency_conflicts": ruled_currency,
        "ruled_currency_conflicts_by_property": currency_conflicts,
        "no_new_conflicts": ruled == 0 and ruled_currency == 0,
        "previous": {"recorded": bool(recorded), "gap_codes": recorded},
        "ratio_to_previous": {code: _ratio(gap_codes[code], n) for code, n in recorded.items()},
    }


def previous_gap_codes(path: str | None) -> dict[str, Any]:
    from scripts.mirror_measure import _previous_fields

    codes = _previous_fields(path, CHECK).get("gap_codes")
    return dict(codes) if isinstance(codes, Mapping) else {}


def run(directory: str | None, previous_report: str | None,
        environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    from integrations.bc_read import session_fresh_since
    from scripts.ontology_projection_probe import open_session

    opened = open_session(directory, session_fresh_since(environ))
    if opened is None:
        return {"probe": PROBE, "error": "session_projection_unavailable",
                "session_projection": {"opened": False}}
    manifest = opened["manifest"]
    sources = sorted(str(s) for s in manifest.get("sources", []))
    out = summarize(opened["generation"].report, previous_gap_codes(previous_report))
    out.update({"session_projection": {"opened": True,
                                       "mode": getattr(opened["generation"], "mode", None)},
                "sources": sources,
                "blocked_upstream": sorted(str(s) for s in manifest.get("blocked_upstream", []))})
    if not sources:
        out["sources_blocked"] = ["portal_api", "bc"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.money_units_probe")
    parser.add_argument("--session-projection", required=True)
    parser.add_argument("--previous-report")
    args = parser.parse_args(argv)
    out = run(args.session_projection, args.previous_report)
    print(json.dumps(out, sort_keys=True))
    return 1 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())
