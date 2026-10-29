"""Actual duty and inbound freight from recorded Sage AP charges, and HS proposals, built from
live read-only relations.

    python3 -m scripts.duty_actual_report OUT_DIR [--state-dir STATE_DIR]

Key and URL come from PLX_WORKBENCH_API_KEY / PLX_WORKBENCH_API_URL and are never printed.
Writes ``duty_actual.json``, ``freight_actual.json``, ``hs_proposals.json`` and
``landed_actuals.json`` into OUT_DIR (a capture store, never Git); with ``--state-dir`` it also
puts ``landed_actuals.json`` where that host's landed cost reads it
(``<STATE_DIR>/landed-actuals/``, ``duty_actual.ActualsFile``). Prints counts only. GET only,
through the Workbench read client. Evidence label: live-unverified (the verifier records a
live result; ``devloop/live-checks/landed_cost.recorded_actuals.json``).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from integrations.workbench_read_api import WorkbenchReadApiClient
from operating import duty_actual as da
from operating import hs_proposals as hp

COLUMNS = {
    "sage.gl_account": ["acctid", "acctdesc"],
    "sage.ap_vendor": ["vendorid", "vendname"],
    "sage.ap_pjd": ["idvend", "idinvc", "idacct", "amtextndhc", "glref", "gldesc", "descomp",
                    "glcomment", "dateinvc"],
    "sage.ap_pjh": ["idvend", "idinvc", "desc", "textref", "dateinvc"],
    "sage.ap_ibh": ["idvend", "idinvc", "ponbr", "ordrnbr", "invcdesc", "textrmit", "texttrx",
                    "dateinvc"],
    "sage.ap_obs": ["idvend", "idinvc", "idponbr", "idordrnbr"],
    "sage.po_receipt_header": ["ponumber", "invnumber", "currency", "extended", "rate",
                               "multipor", "date"],
    "sage.po_header": ["ponumber", "vdcode", "vdcountry", "date"],
    "public.bc_purchase_invoice_snapshot": ["number", "order_number", "vendor_number",
                                            "vendor_name", "vendor_invoice_number",
                                            "amount_excluding_tax", "currency_code",
                                            "invoice_date"],
    "public.app_purchase_order": ["id", "po_number", "currency", "subtotal", "supplier_id",
                                  "incoterms", "date_generated", "created_at"],
    "public.app_supplier": ["id", "incoterms", "country"],
    "public.app_purchase_order_line": ["purchase_order_id", "product_id", "total_price"],
    "sage.ic_item": ["itemno", "desc", "tariffcode"],
    "public.app_product": ["id", "code", "name", "description", "hs_code"],
}


def read_all(client: WorkbenchReadApiClient) -> dict[str, list[dict[str, Any]]]:
    served = client.relations()
    rows: dict[str, list[dict[str, Any]]] = {}
    for name, wanted in COLUMNS.items():
        have = set(served.get(name, {}).get("columns") or ())
        use = [c for c in wanted if c in have]
        rows[name] = client.read_relation(name, columns=use, max_rows=1_000_000,
                                          max_pages=1000, page_size=5000)["rows"] if use else []
    return rows


def duty_report(rows: Mapping[str, Any], kind: str = "duty") -> dict[str, Any]:
    """Charges of ``kind`` linked to POs, the effective rate per PO and counts."""
    built = da.build_charges(rows, kind=kind)
    receipts = rows.get("sage.po_receipt_header", ())
    pos = {k: {"space": "sage"} for k in
           (da.po_key(r.get("ponumber")) for r in rows.get("sage.po_header", ())) if k}
    goods = da.sage_po_goods(receipts)
    portal_lines: dict[str, list[dict[str, Any]]] = {}
    by_id = {}
    for r in rows.get("public.app_purchase_order", ()):
        key = da.po_key(r.get("po_number"))
        if key:
            pos.setdefault(key, {"space": "portal"})
            by_id[str(r.get("id"))] = key
            if key not in goods:
                goods[key] = {"value": r.get("subtotal"), "currency": r.get("currency"),
                              "source": "public.app_purchase_order.subtotal"}
    for r in rows.get("public.app_purchase_order_line", ()):
        key = by_id.get(str(r.get("purchase_order_id")))
        if key:
            portal_lines.setdefault(key, []).append({"item": r.get("product_id")})
    links = da.link_charges(built["charges"], pos, da.supplier_invoice_index(receipts))
    rates = da.po_effective_rates(links, goods, kind=kind)
    reasons = Counter(l.get("reason") for l in links if not l["linked"])
    linked = [l for l in links if l["linked"]]
    usable = {k: v for k, v in rates.items() if v.get("rate") is not None}
    items = da.item_rates(usable, portal_lines)
    return {"links": links, "po_rates": rates, "item_rates": items, "built": built,
            "known_pos": sorted(pos), "summary": {
        "coverage": built["coverage"], "excluded": built["excluded"],
        "charges_linked": len(linked), "charges_unlinked": len(links) - len(linked),
        "unlinked_reasons": dict(reasons),
        "linked_by": dict(Counter(k for l in linked for k in l["linked_by"])),
        f"pos_with_linked_{kind}": len(rates), "pos_with_actual_rate": len(usable),
        "po_rate_gaps": dict(Counter(v["gap"] for v in rates.values() if v.get("gap"))),
        "items_with_actual_rate": len(items),
        "mixed_with_brokerage": sum(1 for v in usable.values() if v["includes_brokerage"])}}


def proposal_report(rows: Mapping[str, Any]) -> dict[str, Any]:
    items = [{"item": f"sage:{r.get('itemno')}", "description": r.get("desc"),
              "hs_code": r.get("tariffcode")} for r in rows.get("sage.ic_item", ())]
    items += [{"item": f"portal:{r.get('id')}",
               "description": " ".join(str(r.get(c) or "") for c in ("name", "description")),
               "hs_code": r.get("hs_code")} for r in rows.get("public.app_product", ())]
    return hp.propose_for_items(items)


def history_report(rows: Mapping[str, Any], duty: Mapping[str, Any],
                   freight: Mapping[str, Any]) -> dict[str, Any]:
    """Estimates from PLX history per kind: from the POs' linked effective rates (per
    supplier, per country, company-wide) and the company-wide aggregate ratio of the charges
    that name no PO over the goods received, per quarter."""
    parties = da.po_parties(rows)
    receipts = rows.get("sage.po_receipt_header", ())
    history = {kind: {"linked": da.history_from_rates(got["po_rates"], parties, kind),
                      "aggregate": da.aggregate_ratio(got.get("built", {}).get("charges", ()),
                                                      receipts, kind)}
               for kind, got in (("duty", duty), ("freight", freight))}
    estimates, default = da.resolve_estimates(parties, history)
    return {"parties": parties, "history": history, "estimates": estimates, "default": default}


def history_summary(hist: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and company-wide ratios only: never a supplier, country or PO and its rate."""
    out: dict[str, Any] = {"pos_with_party": len(hist["parties"]),
                           "pos_with_supplier_or_country_estimate": {
                               k: sum(1 for e in hist["estimates"].values() if k in e)
                               for k in da.KINDS}}
    for kind in da.KINDS:
        got = hist["history"][kind]
        default = hist["default"].get(kind)
        out[kind] = {"linked": got["linked"]["counts"], "aggregate": got["aggregate"]["counts"],
                     "default": None if default is None else {
                         k: default[k] for k in ("rate", "method", "n", "unit", "period",
                                                 "median", "q1", "q3")}}
    return out


def landed_actuals(rows: Mapping[str, Any], duty: Mapping[str, Any],
                   freight: Mapping[str, Any], *, built_at: str,
                   history: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The artifact the live landed cost reads (``duty_actual.build_artifact``): rates,
    counts and codes per PO, the incoterm the PO and its supplier record and, with
    ``history`` (``history_report``), the history estimates."""
    incoterms = da.incoterms_by_po(rows)
    return da.build_artifact({"duty": duty["po_rates"], "freight": freight["po_rates"]},
                             incoterms, sorted(set(duty["known_pos"]) | set(incoterms)),
                             built_at=built_at,
                             estimates=(history or {}).get("estimates"),
                             history_default=(history or {}).get("default"))


def artifact_summary(doc: Mapping[str, Any]) -> dict[str, int]:
    """Counts only: POs with a usable duty or freight rate, with both, and with an incoterm."""
    recs = doc["records"].values()
    usable = {k: sum(1 for r in recs if (r.get(k) or {}).get("rate") is not None)
              for k in da.KINDS}
    return {"records": len(doc["records"]), "known_pos": len(doc["known_pos"]),
            "pos_with_duty_rate": usable["duty"], "pos_with_freight_rate": usable["freight"],
            "pos_with_both": sum(1 for r in recs if all((r.get(k) or {}).get("rate") is not None
                                                         for k in da.KINDS)),
            "pos_with_incoterm": sum(1 for r in recs if r.get("incoterm"))}


def write_artifact(doc: Mapping[str, Any], directory: Path) -> Path:
    """Write the artifact atomically (a reader never sees half a file)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / da.ARTIFACT_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: list[str]) -> int:
    import argparse
    import datetime as dt
    parser = argparse.ArgumentParser(prog="duty_actual_report")
    parser.add_argument("out")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv[1:])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = WorkbenchReadApiClient(os.environ["PLX_WORKBENCH_API_URL"],
                                    os.environ["PLX_WORKBENCH_API_KEY"])
    rows = read_all(client)
    duty, props = duty_report(rows), proposal_report(rows)
    freight = duty_report(rows, "freight")
    for name, got in (("duty_actual.json", duty), ("freight_actual.json", freight)):
        keep = {k: v for k, v in got.items() if k not in ("built", "known_pos")}
        (out / name).write_text(json.dumps(keep, indent=1, default=str), encoding="utf-8")
    (out / "hs_proposals.json").write_text(json.dumps(props, indent=1), encoding="utf-8")
    hist = history_report(rows, duty, freight)
    doc = landed_actuals(rows, duty, freight,
                         built_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                         history=hist)
    write_artifact(doc, out)
    if args.state_dir:
        write_artifact(doc, Path(args.state_dir) / da.ARTIFACT_DIR)
    print(json.dumps({"duty": duty["summary"], "freight": freight["summary"],
                      "landed_actuals": artifact_summary(doc),
                      "history": history_summary(hist),
                      "hs_proposals": props["coverage"]}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
