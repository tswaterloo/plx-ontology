"""HS category table, HS x origin tariff table and the incoterm rule for landed cost.

Owner rules (Tanush, 2026-10-22): an incoterm is a vendor default, overridable per PO, and it
decides title transfer and who bears freight, duty and brokerage. The HS code is per item
CATEGORY. Duty = HS code x origin x destination (Canada). A US origin pays duty only on hair
conditioner and hair serum; otherwise 0. The Portal ``tariff_rate`` (6.5%) is only a labelled
default fallback.

Both tables are built at capture time from recorded values only and are never committed.
Where a category has several recorded codes the table keeps every code with its sources and
flags a conflict; a category with none stays a named gap. Nothing is guessed.

Chain a duty cites: category -> HS code -> (HS code, origin) rate -> duty, each with a source.
Evidence label: isolated-fixture in tests; the live build is the capture script's output.
"""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping, Sequence

import json
from pathlib import Path

RULES_PATH = Path(__file__).with_name("hs_tariff_rules.json")
PROVISIONAL = "provisional (Vince meeting 2026-10-22, to confirm)"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    """The owner rules (one JSON file, edited without code changes). Recorded data always
    wins over them; every answer that relies on one carries its status."""
    return json.loads((path or RULES_PATH).read_text(encoding="utf-8"))


RULES = load_rules()
DESTINATION = RULES["destination"]

_ORIGIN_NAMES = {"CHINA": "CN", "PEOPLES REPUBLIC OF CHINA": "CN", "PRC": "CN", "CN": "CN",
                 "USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
                 "US": "US", "FRANCE": "FR", "FR": "FR", "CANADA": "CA", "CA": "CA"}
_HS = re.compile(r"\d{4}(?:[. ]?\d{2}){0,3}")
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d{1,6})?)\s*%")


def norm_category(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value).strip().lower()) if value is not None else ""
    return text or None


def norm_origin(value: Any) -> str | None:
    """ISO-2 for a recorded origin; None when blank or not a name we know (never guessed)."""
    text = re.sub(r"\s+", " ", str(value or "").strip().upper().replace(".", ""))
    return _ORIGIN_NAMES.get(text)


def norm_hs(value: Any) -> str | None:
    text = str(value or "").strip()
    if not _HS.fullmatch(text):
        return None
    return re.sub(r"\D", "", text)


def norm_rate(value: Any) -> Decimal | None:
    """A recorded tariff as a fraction, only when written with a percent sign."""
    match = _PERCENT.fullmatch(str(value or "").strip())
    if not match:
        return None
    rate = Decimal(match.group(1)) / 100
    return rate if rate <= 1 else None


# ---- table 1: category -> HS code --------------------------------------------------------

def build_hs_category_table(observations: Sequence[Mapping[str, Any]],
                            categories: Sequence[Any] = ()) -> dict[str, Any]:
    """``observations``: ``{category, hs_code, source}`` rows read from the sources (a blank
    code is no observation). ``categories``: every category seen, so one with no code is a
    named gap. Entry status: ``mapped`` (one code), ``conflict`` (several, all kept) or
    ``gap``."""
    entries: dict[str, dict[str, Any]] = {}
    for cat in categories:
        key = norm_category(cat)
        if key:
            entries.setdefault(key, {"codes": {}})
    for obs in observations:
        key, code = norm_category(obs.get("category")), norm_hs(obs.get("hs_code"))
        if key is None or code is None:
            continue
        sources = entries.setdefault(key, {"codes": {}})["codes"].setdefault(code, [])
        if obs.get("source") and obs["source"] not in sources:
            sources.append(obs["source"])
    table: dict[str, Any] = {}
    for key, entry in sorted(entries.items()):
        codes = [{"hs_code": c, "sources": s} for c, s in sorted(entry["codes"].items())]
        status = "gap" if not codes else "mapped" if len(codes) == 1 else "conflict"
        table[key] = {"status": status, "codes": codes}
    return {"categories": table, "coverage": {
        "categories": len(table),
        "mapped": sum(v["status"] == "mapped" for v in table.values()),
        "conflict": sum(v["status"] == "conflict" for v in table.values()),
        "gap": sum(v["status"] == "gap" for v in table.values())}}


# ---- table 2: (HS code, origin) -> rate to Canada ------------------------------------------

def build_tariff_table(observations: Sequence[Mapping[str, Any]],
                       rules: Mapping[str, Any] = RULES) -> dict[str, Any]:
    """``observations``: ``{hs_code, origin, rate, source}`` from recorded ``tariff_c`` /
    ``tariffcode`` values; only a rate written with ``%`` and a known origin counts. Several
    different recorded rates for one (HS, origin) are kept and flagged a conflict. The US rule
    and the default fallback are explicit labelled entries, not recorded rates."""
    rates: dict[str, dict[str, Any]] = {}
    skipped = 0
    for obs in observations:
        hs, origin, rate = norm_hs(obs.get("hs_code")), norm_origin(obs.get("origin")), \
            norm_rate(obs.get("rate"))
        if hs is None or origin is None or rate is None:
            skipped += 1
            continue
        found = rates.setdefault(f"{hs}|{origin}", {"hs_code": hs, "origin": origin, "rates": {}})
        sources = found["rates"].setdefault(str(rate), [])
        if obs.get("source") and obs["source"] not in sources:
            sources.append(obs["source"])
    recorded = []
    for key, v in sorted(rates.items()):
        recorded.append({"hs_code": v["hs_code"], "origin": v["origin"],
                         "status": "recorded" if len(v["rates"]) == 1 else "conflict",
                         "rates": [{"rate": r, "sources": s} for r, s in sorted(v["rates"].items())]})
    return {"destination": DESTINATION, "recorded": recorded,
            "owner_rules": rules["origin_duty_rules"], "default": rules["default_tariff"],
            "coverage": {"recorded_pairs": len(recorded), "unusable_observations": skipped}}


# ---- duty citation ------------------------------------------------------------------------

def cite_duty(category: Any, origin: Any, hs_table: Mapping[str, Any],
              tariff_table: Mapping[str, Any], rules: Mapping[str, Any] = RULES
              ) -> dict[str, Any]:
    """The cited duty rate for an item: category -> HS -> (HS, origin) rate. Returns
    ``{status, rate, chain, provisional_rules}``. Recorded data wins: a recorded rate for the
    category's HS code and the origin is used whatever a rule says. ``status`` is ``cited``
    (a recorded rate, or an owner origin rule), ``default`` (the labelled fallback; never
    presented as a classification) or ``gap`` (``gap`` names what is missing). Every answer
    that relies on a rule or the default lists it in ``provisional_rules`` with its status."""
    key, org = norm_category(category), norm_origin(origin)
    chain: list[dict[str, Any]] = []
    if org is None:
        return {"status": "gap", "gap": "ORIGIN_NOT_RECORDED", "rate": None, "chain": chain,
                "provisional_rules": []}
    entry = (hs_table.get("categories") or {}).get(key) if key else None
    code = entry["codes"][0] if entry and entry["status"] == "mapped" else None
    pair = next((r for r in tariff_table.get("recorded", [])
                 if code and r["hs_code"] == code["hs_code"] and r["origin"] == org), None)
    if code:
        chain.append({"step": "category_hs", "category": key, "hs_code": code["hs_code"],
                      "sources": code["sources"]})
    if pair is not None and pair["status"] == "recorded":
        got = pair["rates"][0]
        chain.append({"step": "hs_origin_rate", "hs_code": code["hs_code"], "origin": org,
                      "rate": got["rate"], "sources": got["sources"]})
        return {"status": "cited", "rate": Decimal(got["rate"]), "hs_code": code["hs_code"],
                "chain": chain, "provisional_rules": []}
    if pair is not None:
        return {"status": "gap", "gap": "TARIFF_RATE_CONFLICT", "rate": None, "chain": chain,
                "rates": pair["rates"], "provisional_rules": []}
    for rule in rules.get("origin_duty_rules", ()):
        if rule["origin"] != org:
            continue
        if key is None:
            return {"status": "gap", "gap": "CATEGORY_NOT_RECORDED", "rate": None,
                    "chain": chain, "provisional_rules": []}
        if key not in rule["dutiable_categories"]:
            chain.append({"step": "origin_rule", "rule": rule["id"], "origin": org,
                          "category": key, "rate": rule["otherwise_rate"],
                          "source": rule["source"], "status": rule["status"]})
            return {"status": "cited", "rate": Decimal(rule["otherwise_rate"]),
                    "hs_code": code["hs_code"] if code else None, "chain": chain,
                    "provisional_rules": [{"id": rule["id"], "status": rule["status"]}]}
    if entry is None:
        return {"status": "gap", "gap": "CATEGORY_NOT_RECORDED", "rate": None, "chain": chain,
                "provisional_rules": []}
    if entry["status"] == "gap":
        return {"status": "gap", "gap": "HS_CODE_NOT_RECORDED_FOR_CATEGORY", "rate": None,
                "chain": chain, "provisional_rules": []}
    if entry["status"] == "conflict":
        return {"status": "gap", "gap": "HS_CODE_CONFLICT", "rate": None, "chain": chain,
                "codes": entry["codes"], "provisional_rules": []}
    default = tariff_table.get("default") or rules["default_tariff"]
    chain.append({"step": "default_fallback", "rate": default["rate"],
                  "source": default["source"], "status": default["status"]})
    return {"status": "default", "gap": "TARIFF_NOT_RECORDED_FOR_HS_ORIGIN",
            "rate": Decimal(default["rate"]), "hs_code": code["hs_code"], "chain": chain,
            "provisional_rules": [{"id": "default_tariff", "status": default["status"]}]}


def tariff_for_po_line(cited: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``tariff`` argument of ``landed_cost.po_line_landed``: only a cited duty with an
    HS code; a default, a gap or a rule without a code stays unverified there."""
    if cited.get("status") != "cited" or not cited.get("hs_code"):
        return None
    sources = [s for step in cited["chain"] for s in (step.get("sources") or [step.get("source")])
               if s]
    return {"hs_code": cited["hs_code"], "duty_rate": str(cited["rate"]),
            "_source": "; ".join(sources), "chain": cited["chain"],
            "provisional_rules": cited["provisional_rules"]}


# ---- incoterm rule ------------------------------------------------------------------------

def resolve_incoterm(po_incoterm: Any, vendor_default: Any,
                     rules: Mapping[str, Any] = RULES) -> dict[str, Any]:
    """The PO's incoterm overrides the vendor default. ``bearing`` is None (a gap) when none
    is recorded or the term is not one the rules state: never defaulted. The answer carries
    the rule's provisional status."""
    base = {"source": rules["incoterm_source"], "status": rules["incoterm_status"]}
    for value, where in ((po_incoterm, "po"), (vendor_default, "vendor_default")):
        text = str(value or "").strip().upper()
        if text:
            rule = rules["incoterms"].get(text)
            if rule is None:
                return {**base, "incoterm": text, "set_by": where, "bearing": None,
                        "gap": "INCOTERM_RULE_NOT_RECORDED"}
            return {**base, "incoterm": text, "set_by": where, "title_transfer": rule["title"],
                    "buyer_bears": {c: c in rule["buyer_bears"]
                                    for c in ("freight", "duty", "brokerage")},
                    "in_transit_inventory_from_title_transfer": True, "bearing": True}
    return {**base, "incoterm": None, "set_by": None, "bearing": None,
            "gap": "INCOTERM_NOT_RECORDED"}



# ---- capture ------------------------------------------------------------------------------

def observations_from_rows(rows: Mapping[str, Sequence[Mapping[str, Any]]]
                           ) -> tuple[list[dict], list[Any], list[dict]]:
    """Recorded values from the read relations -> (hs observations, categories seen, tariff
    observations). A blank value is no observation."""
    hs_obs: list[dict] = []
    cats: list[Any] = []
    products = {str(p.get("id")): p for p in rows.get("public.app_product", ())}
    for p in products.values():
        for col in ("category_id", "sage_category"):
            if p.get(col):
                cats.append(p[col])
                if p.get("hs_code"):
                    hs_obs.append({"category": p[col], "hs_code": p["hs_code"],
                                   "source": f"public.app_product.hs_code by {col}"})
    for item in rows.get("sage.ic_item", ()):
        if item.get("category"):
            cats.append(item["category"])
            if item.get("tariffcode"):
                hs_obs.append({"category": item["category"], "hs_code": item["tariffcode"],
                               "source": "sage.ic_item.tariffcode by category"})
    tariff_obs: list[dict] = []
    for rel in ("public.raw_material_supplier", "public.packaging_supplier"):
        for row in rows.get(rel, ()):
            # FileMaker fields hscode / tariff_c / countryoforigin: a recorded rate counts
            # only on a row that states all three (the served columns today omit two).
            if row.get("tariff_c") and row.get("hscode") and row.get("countryoforigin"):
                tariff_obs.append({"hs_code": row["hscode"], "origin": row["countryoforigin"],
                                   "rate": row["tariff_c"], "source": f"{rel}.tariff_c"})
    # Portal origin per product joins an HS code to an origin; its rate must come from tariff_c.
    return hs_obs, cats, tariff_obs
