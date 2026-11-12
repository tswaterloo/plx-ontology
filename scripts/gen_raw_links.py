"""Generate the links of the raw Portal, FileMaker-silver and Sage row types to the business
records they name (coverage gap 6: raw ``Rel*`` types no business link reached).

The links come from column metadata, never from a case: the pinned Portal reporting-API
catalog (``ontology/schema/generated/meta/portal_catalog.json``) lists every relation's
columns, and :data:`KEY_RULES` say what a natural-key column names (a Sage customer code,
invoice, order, shipment or PO number, vendor code, GL account or batch) and
:data:`SILVER_TWINS` which FileMaker relation a silver table copies under the same id.
Every served raw type whose relation carries such a column gets one ``fk`` link type per
rule, written to one curated domain file per source family (``raw_links_<family>.json``,
grouped by domain: Sage AR, Sage AP, Sage GL, Portal silver/gold). A link is written only
where both ends are served types of the compiled schema and the to-binding's relation
holds the to-column; a column a curated type already binds (the raw type is shadowed) is
the curated type's business. Run::

    python3 -m scripts.gen_raw_links generate   # (re)write the files
    python3 -m scripts.gen_raw_links check      # exit 1 when a file differs

Stdlib only. No network, no store read."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DOMAINS = ROOT / "ontology" / "schema" / "domains"
CATALOG = ROOT / "ontology" / "schema" / "generated" / "meta" / "portal_catalog.json"
PREFIX = "raw_links_"
SELF = "scripts/gen_raw_links.py"

# family -> (relation globs, description)
FAMILIES: dict[str, tuple[tuple[str, ...], str]] = {
    "sage_ar": (("sage.ar_*",), "Sage 300 Accounts Receivable rows (batches, invoice "
                "entries, posting journals, open documents and their schedules, statements, "
                "customer statistics) linked to the customer, sales invoice, order and "
                "shipment they name, to their entry header and to the GL account."),
    "sage_ap": (("sage.ap_*",), "Sage 300 Accounts Payable rows (batches, invoice entries, "
                "posting journals, open documents, vendor statistics) linked to the Sage "
                "vendor and purchase order they name, to their entry header and to the GL "
                "account; the Sage vendor to the Portal supplier holding its code."),
    "sage_gl": (("sage.gl_*",), "Sage 300 General Ledger rows (journal entries and their "
                "details, posting journals, batches, account statistics) linked to their "
                "batch, entry and GL account."),
    "portal_raw": (("silver.*", "public.*"), "Portal silver tables (FileMaker copies keyed by "
                   "the FileMaker id) linked to the FileMaker record they copy, and Portal "
                   "gold rows linked by their natural keys."),
}

# A natural-key rule: a column (any of ``columns``) of a relation matching ``relations``
# names the record of ``to_type`` whose ``to_binding`` row holds it at ``to_path``.
# ``tier``: verified when both rows come from one system's own numbering (Sage to Sage);
# corroborated is never claimed here; a cross-system code match is a lead with
# unique_target (an ambiguous code links nothing). ``where``: relation -> the predicates a
# row must pass for the rule to link it (the binding's ``where``).
KEY_RULES: tuple[dict[str, Any], ...] = (
    {"relations": ("sage.ar_*",), "columns": ("idcust",), "name": "customer",
     "to_type": "Customer", "to_binding": "portal.sage_ar_customer", "to_path": "idcust",
     "compare": "identifier", "kind": "customer_erp_code", "tier": "verified",
     "why": "the Sage customer number (AR IDCUST) of the row"},
    {"relations": ("sage.ar_*",), "except": ("sage.ar_ibd",), "columns": ("idinvc",),
     "name": "sales_invoice",
     "to_type": "SalesInvoice", "to_binding": "portal.sage_oe_invoice_header",
     "to_path": "invnumber", "compare": "identifier", "kind": "invoice_number",
     "tier": "verified", "why": "the AR document number, the OE invoice number OE posts "
     "to AR under (Sage OE->AR integration)",
     # aged rows of credit notes, receipts and adjustments (document types 3, 10, 11, 14)
     # carry their own document numbers, never an OE invoice's (audit 2026-11-08: type 1
     # reaches an invoice 6,178 of 6,215, 15 of 15 sampled pairs on the customer; others 0)
     "where": {"sage.ar_aged": [{"path": "doctype", "op": "in", "value": [1, "1"]}]}},
    {"relations": ("sage.ar_*",), "columns": ("idordernbr", "idordrnbr", "ordrnbr"),
     "name": "sales_order", "to_type": "SalesOrder",
     "to_binding": "portal.sage_oe_order_header", "to_path": "ordnumber",
     "compare": "identifier", "kind": "so_number", "tier": "verified",
     "why": "the OE order number the AR document carries"},
    # text_id, not the shipment_number grammar: that kind is the Portal series (SH + 6-9
    # digits) and rejects every Sage shipment number (two letters + 10 digits), so the
    # identifier compare linked none (live 2026-11-11: 0 links). Both columns are Sage's
    # own CHAR(22), padded alike, so the exact text is the key.
    {"relations": ("sage.ar_*",), "columns": ("idshipnbr",), "name": "shipment",
     "to_type": "Shipment", "to_binding": "portal.sage_oe_shipment_header",
     "to_path": "shinumber", "compare": "text_id", "kind": None,
     "tier": "verified", "why": "the OE shipment number the AR document carries"},
    {"relations": ("sage.ap_*",), "columns": ("idvend", "vendorid"), "name": "vendor",
     "to_type": "RelSageApVendor", "to_binding": "gen.sage.ap_vendor", "to_path": "vendorid",
     "compare": "identifier", "kind": "supplier_code", "tier": "verified",
     "why": "the Sage vendor number (AP IDVEND / VENDORID) of the row"},
    {"relations": ("sage.ap_*",), "columns": ("idponbr",), "name": "purchase_order",
     "to_type": "PurchaseOrder", "to_binding": "portal.sage_po_header",
     "to_path": "ponumber", "compare": "identifier", "kind": "petra_po", "tier": "verified",
     "why": "the Sage PO number the AP document carries"},
    {"relations": ("sage.ap_vendor",), "columns": ("vendorid",), "name": "portal_supplier",
     "to_type": "Supplier", "to_binding": "portal.app_supplier", "to_path": "code",
     "compare": "identifier", "kind": "supplier_code", "tier": "lead", "unique": True,
     "why": "the Sage vendor number equal to one Portal supplier's code (two systems' "
     "codes: a lead until a review or a second key confirms it)"},
    {"relations": ("sage.ar_*", "sage.ap_*", "sage.gl_*"),
     "columns": ("acctid",), "name": "gl_account",
     "to_type": "RelSageGlAccount", "to_binding": "gen.sage.gl_account", "to_path": "acctid",
     "compare": "identifier", "kind": "gl_account", "tier": "verified",
     "why": "the Sage GL account key (ACCTID, unformatted) of the row"},
    # AR/AP rows carry the formatted account. It is compared with every account's formatted
    # number (ACCTFMTTD), the form such a value is read in, so two accounts whose numbers
    # meet in that form are both candidates: the value is ambiguous and no verified link
    # is written (a lead naming both, ambiguous_target). On 2026-11-08 the four accounts
    # whose ACCTID carries two digits past the formatted number (9999901000001 shows as
    # 99999-01-0000) were the only ones whose two forms differ, and one of them shares its
    # formatted number with ACCTID 99999010000.
    {"relations": ("sage.ar_*", "sage.ap_*"), "columns": ("idglacct", "idacct", "idacctrev"),
     "name": "gl_account", "name_by_column": True, "to_type": "RelSageGlAccount",
     "to_binding": "gen.sage.gl_account", "to_path": "acctfmttd", "compare": "identifier", "kind": "gl_account", "tier": "verified",
     "why": "the formatted Sage GL account an AR/AP row carries, compared with every "
     "account's formatted number (a number two accounts share is ambiguous)"},
    {"relations": ("sage.gl_*",), "columns": ("batchnbr", "batchid"), "name": "gl_batch",
     "to_type": "RelSageGlBatch", "to_binding": "gen.sage.gl_batch", "to_path": "batchid",
     "compare": "integer_id", "kind": None, "tier": "verified",
     "why": "the GL batch number of the row"},
    {"relations": ("public.app_batch_schedule",), "columns": ("batch_name",),
     "name": "batch_by_code", "to_type": "Batch", "to_binding": "portal.fm_batch",
     "to_path": "namebatch_c", "compare": "identifier", "kind": "batch_code",
     "tier": "lead", "unique": True,
     "why": "the batch code of a Portal batch-schedule row equal to one FileMaker batch's "
     "(the row's batch_id is blank)"},
)

# Header rules: a detail row names its entry header by the batch/entry counters Sage keys
# both by (same module, same numbering: verified).
HEADER_RULES: tuple[dict[str, Any], ...] = tuple(
    {"from": f"sage.{m}_{d}", "to": f"sage.{m}_{h}", "name": name,
     "pairs": tuple((c, c) for c in cols)}
    for m in ("ar", "ap")
    for d, h, name, cols in (
        ("ibd", "ibh", "invoice_entry", ("cntbtch", "cntitem")),
        ("ibs", "ibh", "invoice_entry", ("cntbtch", "cntitem")),
        ("ibh", "ibc", "invoice_batch", ("cntbtch",)),
        ("pjd", "pjh", "posted_entry", ("postseqnce", "cntbtch", "cntitem")),
    )) + (
    {"from": "sage.gl_je_detail", "to": "sage.gl_je_header", "name": "journal_entry",
     "pairs": (("batchnbr", "batchid"), ("journalid", "btchentry"))},
)

# silver table -> (curated FileMaker binding holding the same records under the same id,
# its type). The Portal's silver schema is its cleaned copy of FileMaker keyed by the
# FileMaker id (the skeleton's FileMaker link contracts join silver ids to FM ids).
SILVER_TWINS: dict[str, tuple[str, str]] = {
    "silver.suppliers": ("portal.fm_supplier", "Supplier"),
    "silver.customers": ("portal.fm_customer", "Customer"),
    "silver.manufacturers": ("portal.fm_manufacturer", "Manufacturer"),
    "silver.raw_materials": ("portal.fm_raw_material", "Product"),
    "silver.packaging": ("portal.fm_packaging", "Product"),
    "silver.bulk_products": ("portal.fm_bulk_product", "Product"),
    "silver.finished_products": ("portal.fm_finished_product", "Product"),
    "silver.finished_product_items": ("portal.fm_finished_product_item", "ProductComponent"),
    "silver.raw_material_suppliers": ("portal.fm_raw_material_supplier", "SupplierItem"),
    "silver.packaging_suppliers": ("portal.fm_packaging_supplier", "SupplierItem"),
    "silver.purchase_orders": ("portal.fm_generated_purchase_order", "PurchaseOrder"),
    "silver.purchase_order_items": ("portal.fm_generated_purchase_order_item",
                                    "PurchaseOrderLine"),
    "silver.receipts": ("portal.fm_receipt", "Receipt"),
    "silver.receipt_raw_items": ("portal.fm_receipt_raw_material", "ReceiptLine"),
    "silver.receipt_pack_items": ("portal.fm_receipt_packaging", "ReceiptLine"),
    "silver.batches": ("portal.fm_batch", "Batch"),
    "silver.manufacturing_orders": ("portal.fm_assembly_order", "AssemblyOrder"),
    "silver.assembly_order_items": ("portal.fm_assembly_order_item",
                                    "AssemblyOrderComponent"),
    "silver.assembly_order_damage": ("portal.fm_assembly_order_damage",
                                     "AssemblyOrderDamage"),
    "silver.assembly_order_statuses": ("portal.fm_assembly_order_status",
                                       "AssemblyOrderStatusEntry"),
    "silver.order_entries": ("portal.fm_order_entry", "SalesOrder"),
    "silver.order_entry_items": ("portal.fm_order_entry_item", "SalesOrderLine"),
}


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _evidence(note: str) -> list[dict[str, str]]:
    return [{"kind": "workbench_code", "path": SELF, "note": note[:600]},
            {"kind": "workbench_code", "path": CATALOG.relative_to(ROOT).as_posix(),
             "note": "the relation's columns in the pinned reporting-API catalog"}]


def _pair(src: str, dst: str, compare: str, kind: str | None) -> dict[str, str]:
    out = {"from": src, "to": dst, "compare": compare}
    if kind:
        out["identifier_kind"] = kind
    return out


def build(compiled: Any, catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """family -> domain document (format plx-ontology-domain/v1)."""
    columns = {r["name"]: set(r["columns"]) for r in catalog["relations"]}
    raw: dict[str, tuple[str, str]] = {}      # relation -> (raw type, binding id)
    for tid, spec in compiled.object_types.items():
        if not tid.startswith("Rel") or tid in compiled.shadowed or \
                spec.get("availability", {}).get("state") == "not_served":
            continue
        for b in spec.get("bindings", []):
            if b.get("source") == "portal_api":
                raw[b["relation"]] = (tid, b["id"])
    bindings = {bid: (tid, b) for bid, (tid, b) in compiled.bindings.items()}

    def relation_of(binding: str) -> str | None:
        held = bindings.get(binding)
        return held[1].get("relation") if held else None

    links: dict[str, list[dict[str, Any]]] = {f: [] for f in FAMILIES}

    def family(relation: str) -> str:
        return next(f for f, (globs, _d) in FAMILIES.items()
                    if any(fnmatch.fnmatchcase(relation, g) for g in globs))

    def add(relation: str, link: dict[str, Any]) -> None:
        if link["id"] in compiled.link_types:
            return
        links[family(relation)].append(link)

    for relation in sorted(raw):
        tid, binding = raw[relation]
        cols = columns.get(relation, set())
        for rule in KEY_RULES:
            if not any(fnmatch.fnmatchcase(relation, g) for g in rule["relations"]) or \
                    relation in rule.get("except", ()):
                continue
            target = bindings.get(rule["to_binding"])
            to_rel = relation_of(rule["to_binding"])
            if target is None or target[0] != rule["to_type"] or to_rel == relation or \
                    rule["to_path"] not in columns.get(to_rel, set()) or \
                    rule["to_type"] in compiled.shadowed:
                continue
            for col in rule["columns"]:
                if col not in cols:
                    continue
                name = rule["name"] if col == rule["columns"][0] and \
                    not rule.get("name_by_column") else f"{rule['name']}_{col}"
                binding_spec = {
                    "kind": "fk", "tier_ceiling": rule["tier"], "from_binding": binding,
                    "to_binding": rule["to_binding"],
                    "pairs": [_pair(col, rule["to_path"], rule["compare"], rule["kind"])],
                    "evidence": _evidence(f"{relation}.{col}: {rule['why']}.")}
                if rule.get("unique"):
                    binding_spec["unique_target"] = True
                if relation in rule.get("where", {}):   # rows the rule holds for only
                    binding_spec["where"] = list(rule["where"][relation])
                add(relation, {
                    "id": f"{tid}.{name}", "from": tid, "to": rule["to_type"], "name": name,
                    "reverse_name": f"{_snake(tid[3:])}_rows_by_{col}",
                    "cardinality": "N:1",
                    "description": (f"{relation}.{col} = {to_rel}.{rule['to_path']}: "
                                    f"{rule['why']} ({rule['tier']}"
                                    + (", one candidate only" if rule.get("unique") else "")
                                    + "; scripts/gen_raw_links.py)."),
                    "bindings": [binding_spec]})
                break
    for rule in HEADER_RULES:
        if rule["from"] not in raw or rule["to"] not in raw:
            continue
        (ftid, fb), (ttid, tb) = raw[rule["from"]], raw[rule["to"]]
        if not all(a in columns.get(rule["from"], ()) and b in columns.get(rule["to"], ())
                   for a, b in rule["pairs"]):
            continue
        add(rule["from"], {
            "id": f"{ftid}.{rule['name']}", "from": ftid, "to": ttid, "name": rule["name"],
            "reverse_name": _snake(ftid[3:]) + "_rows", "cardinality": "N:1",
            "description": (f"{rule['from']} -> {rule['to']} on "
                            + ", ".join(f"{a} = {b}" for a, b in rule["pairs"])
                            + ": the Sage entry header the row belongs to (verified; "
                              "scripts/gen_raw_links.py)."),
            "bindings": [{"kind": "fk", "tier_ceiling": "verified", "from_binding": fb,
                          "to_binding": tb,
                          "pairs": [_pair(a, b, "integer_id", None) for a, b in rule["pairs"]],
                          "evidence": _evidence(f"{rule['from']} names its "
                                                f"{rule['to']} header by "
                                                + ", ".join(a for a, _ in rule["pairs"]))}]})
    for relation, (to_binding, to_type) in sorted(SILVER_TWINS.items()):
        if relation not in raw or to_binding not in bindings or \
                bindings[to_binding][0] != to_type or "id" not in columns.get(relation, ()):
            continue
        tid, binding = raw[relation]
        fm = relation_of(to_binding)
        add(relation, {
            "id": f"{tid}.fm_record", "from": tid, "to": to_type, "name": "fm_record",
            "reverse_name": _snake(tid[3:]) + "_rows", "cardinality": "1:1",
            "description": (f"{relation}.id = {fm}.id: the FileMaker record this Portal "
                            "silver row copies under the same FileMaker id (verified; "
                            "scripts/gen_raw_links.py SILVER_TWINS)."),
            "bindings": [{"kind": "fk", "tier_ceiling": "verified", "from_binding": binding,
                          "to_binding": to_binding,
                          "pairs": [_pair("id", "id", "integer_id", None)],
                          "evidence": _evidence(f"{relation} is the Portal silver copy of "
                                                f"FileMaker {fm}, keyed by the FileMaker id "
                                                "(the skeleton's FM link contracts join "
                                                "silver ids to FM ids)")}]})
    for relation, spec in sorted(business_joins().items()):
        if relation not in raw:
            raise ValueError(f"business.json {relation}: joins on a relation that is not a "
                             "served raw type")
        tid, binding = raw[relation]
        for join in spec:
            target = bindings.get(join["to_binding"])
            if target is None:
                raise ValueError(f"business.json {relation}.{join['name']}: unknown binding "
                                 f"{join['to_binding']}")
            to_tid, to_b = target
            to_rel = to_b.get("relation")
            to_cols = columns.get(to_rel)
            if to_cols is None:   # a BC entity set: its columns are the binding's paths
                to_cols = {src.get("path") for p in compiled.object_types[to_tid]["properties"]
                           for src in p.get("sources", ()) if src.get("binding") == to_b["id"]}
            missing = [f"{relation}.{p['from']}" for p in join["pairs"]
                       if p["from"] not in columns.get(relation, ())] + \
                [f"{to_rel}.{p['to']}" for p in join["pairs"] if p["to"] not in to_cols]
            if missing:
                raise ValueError(f"business.json {relation}.{join['name']}: no column "
                                 + ", ".join(missing))
            pairs = [_pair(p["from"], p["to"], p["compare"], p.get("identifier_kind"))
                     for p in join["pairs"]]
            on = ", ".join(f"{p['from']} = {to_rel}.{p['to']}" for p in join["pairs"])
            binding_spec = {"kind": "fk", "tier_ceiling": join["tier"],
                            "from_binding": binding, "to_binding": join["to_binding"],
                            "pairs": pairs,
                            "evidence": _evidence(f"{relation} ({on}): {join['why']} "
                                                  "(ontology/schema/overrides/business.json).")}
            if join.get("unique_target"):
                binding_spec["unique_target"] = True
            if join.get("where"):   # rows the join holds for only (e.g. one source ledger)
                binding_spec["where"] = list(join["where"])
            add(relation, {
                "id": f"{tid}.{join['name']}", "from": tid, "to": to_tid, "name": join["name"],
                "reverse_name": f"{_snake(tid[3:])}_rows_by_{join['name']}",
                "cardinality": "N:1",
                "description": (f"{relation}: {on}: {join['why']} ({join['tier']}"
                                + (", one candidate only" if join.get("unique_target") else "")
                                + "; declared in ontology/schema/overrides/business.json)."),
                "bindings": [binding_spec]})
    out = {}
    for fam, items in links.items():
        if not items:
            continue
        out[fam] = {"$schema": "../_format.schema.json", "format": "plx-ontology-domain/v1",
                    "domain": PREFIX + fam, "origin": "curated",
                    "description": FAMILIES[fam][1] + " Generated by scripts/gen_raw_links.py "
                    "from column metadata; regenerate, do not edit.",
                    "link_types": sorted(items, key=lambda i: i["id"])}
    return out


def business_joins() -> dict[str, list[dict[str, Any]]]:
    """relation -> its declared joins (``ontology/schema/overrides/business.json``)."""
    sys.path.insert(0, str(ROOT))
    from ontology.generator.overrides import load_overrides
    return {rel: list(spec["joins"])
            for rel, spec in load_overrides().business.get("portal_api", {}).items()
            if spec.get("joins")}


def _dump(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _compiled() -> Any:
    """The compiled schema without the raw-link files (the rules read the rest of it)."""
    import shutil
    import tempfile
    sys.path.insert(0, str(ROOT))
    from ontology import schema as schema_loader
    src = Path(schema_loader.SCHEMA_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "schema"
        shutil.copytree(src, root, ignore=lambda d, names: [
            n for n in names if n.startswith(PREFIX) and n.endswith(".json")])
        # a binding's marking_rules hop the links these files declare: not read here
        for path in (root / "generated").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            if '"marking_rules"' in text:
                doc = json.loads(text)
                for t in doc.get("object_types", []):
                    for b in t.get("bindings", []):
                        b.pop("marking_rules", None)
                path.write_text(json.dumps(doc), encoding="utf-8")
        return schema_loader.load_schema(root, check_format=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.gen_raw_links")
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    docs = build(_compiled(), json.loads(CATALOG.read_text(encoding="utf-8")))
    stale = []
    for fam, doc in docs.items():
        path = DOMAINS / f"{PREFIX}{fam}.json"
        text = _dump(doc)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        stale.append(path.name)
        if args.command == "generate":
            path.write_text(text, encoding="utf-8")
    extra = sorted(p.name for p in DOMAINS.glob(f"{PREFIX}*.json")
                   if p.stem[len(PREFIX):] not in docs)
    print(json.dumps({"ok": not (stale or extra) or args.command == "generate",
                      "files": {f: len(d["link_types"]) for f, d in docs.items()},
                      "stale": stale, "extra": extra}))
    return 0 if args.command == "generate" or not (stale or extra) else 1


if __name__ == "__main__":
    raise SystemExit(main())
