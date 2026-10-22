"""Facts -> generated ontology types, identity keys, links and markings, for any source.

A source adapter (``scripts/gen_ontology_skeleton.py`` for the Portal reporting API)
reads its own schema and hands ``generate`` source-independent facts; the semantics the
source does not state come from the declarative overrides (``ontology/generator/
overrides.py``) and the Workbench link and unit contracts. Nothing here names an entity.

Facts (a mapping):

* ``source``: the binding source family (``portal_api``, ``bc``, ...);
* ``file_prefix``, ``description``, ``upstream_pins``: the generated domain files' name
  prefix, description and upstream pins; ``name_prefix`` (optional): prefixed to a
  relation name in its type and binding ids (``bc.`` -> ``RelBcSalesQuotes``,
  ``gen.bc.sales_quotes``); ``navigation_taken`` (optional): object type -> the navigation
  names curated links already use on it;
* ``relations``: one per relation the source declares: ``name``, ``domain``,
  ``excluded`` (why it is not bound, else None), ``key``, ``key_source`` (how the source
  chose the key: ``identity.json`` decides whether that is a verified identity),
  ``columns`` (``name``, ``value_type``, optional ``enum_values``), ``description``,
  ``evidence`` (of the relation), ``key_evidence`` (of its key) and ``record_evidence``
  (the citation a column override is noted on, or None); optional ``lineage`` (the
  binding lineage the source states, e.g. ``{"from_capture": "bc_company"}``, else the
  ``lineage.json`` default) and ``note``; optional ``bound_by`` (``object_type`` and
  ``binding`` of the curated binding that reads it): no type is generated for it, but a
  source link to or from it binds through that curated binding (curation wins);
* ``links``: the source's own foreign keys, in order: ``from``, ``to`` (relations),
  ``field`` (the link's name), ``columns``, ``references``, ``compares``, ``unique``
  (the columns are a key of ``from``: 1:1), ``reverse`` (the target's name for it, or
  None), ``description``, ``evidence``, optional ``identifier_kinds`` (per pair, for an
  ``identifier`` compare) and ``tier`` (the binding's tier ceiling, default verified);
  a link whose two ends are curated is left to the curated files;
* ``unserved`` (optional): the relations the source declares but does not serve, each
  ``relation``, ``domain`` (its generated file), ``upstream_name`` (the source's own name
  for it, or None), ``reason``, ``upstream_ask``, ``description``, ``evidence`` and
  ``declared`` (``object_type`` and ``file`` of the curated declaration that already
  stands for it, or None).

Raw types are ``Rel<Relation>`` with one binding ``gen.<relation>``; a link is bound only
between bound relations over served columns, and a declared link contract on the same
column pairs joins it rather than adding a second link. An unserved relation with no
curated declaration is a ``Rel<Relation>`` type (or the curated id ``names.json`` gives the
relation, with its anchor and markings) with ``availability`` ``not_served``, its reason and
upstream ask, and no binding, property or link (``<file_prefix><domain>.json``):
a foreign key to it stays a plain value of the served column and names it as its
``target_type``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from ontology import schema as ontology_schema
from ontology.generator.overrides import OverrideError, Overrides

LINK_CONTRACTS = "operating/link_contracts.json"
UNIT_CONTRACTS = "operating/source_unit_contracts.json"
PARSERS = {"text": "as_is", "integer": "integer_text", "decimal": "decimal_text",
           "boolean": "boolean_text", "timestamp": "iso_timestamp", "date": "iso_date",
           "json": "as_is", "enum": "as_is"}
TIER_ORDER = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead")


def pascal(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text or not text[0].isalpha():
        text = "x_" + text
    return text[:64]


def type_id(relation_name: str, prefix: str = "") -> str:
    return ("Rel" + pascal(prefix + relation_name))[:64]


def binding_id(relation_name: str, prefix: str = "") -> str:
    """``gen.<relation>``; a prefixed source (camelCase entity names) uses the snake form,
    as binding ids are lower case: ``gen.bc.sales_quotes``."""
    return "gen." + (prefix + snake(relation_name) if prefix else relation_name)


def property_name(column: str, taken: set[str]) -> str:
    name = snake(column)
    while name in taken:
        name = (name + "_col")[:64]
    taken.add(name)
    return name


def generate(facts: Mapping[str, Any], link_contracts: Mapping[str, Any],
             unit_contracts: Mapping[str, Any], fm_source_fk: Mapping[str, tuple[str, str]],
             overrides: Overrides) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ({file name: domain document}, report). Deterministic.

    The report holds ``type_of_relation``, ``excluded``, ``bound_elsewhere`` (relation ->
    the curated ``object_type`` and ``binding`` standing for it), ``not_served`` (unserved
    relation -> ``object_type``, ``upstream_name`` and ``declared_by``: ``generated`` or
    the curated file), ``links_not_bound`` (a link whose target relation is not served
    carries its ``target_type`` and the ``link`` a pending binding of that relation
    gets), ``link_contracts``, ``fm_source_fk``, ``unit_contracts``, ``money`` (declared
    money columns, then the name-rule markings) and ``counts``."""
    prefix = facts.get("name_prefix", "")
    relations = {r["name"]: r for r in facts["relations"]}
    columns_of = {name: [c["name"] for c in r["columns"]] for name, r in relations.items()}
    excluded: dict[str, str] = {}
    types: dict[str, dict[str, Any]] = {}
    type_of_relation: dict[str, str] = {}
    # relation -> (type id, binding id) a link to or from it binds: generated or curated.
    ends: dict[str, tuple[str, str]] = {}
    bound_elsewhere: dict[str, dict[str, str]] = {}
    prop_of: dict[tuple[str, str], str] = {}
    identity = overrides.identity
    for name in sorted(relations):
        rel = relations[name]
        if rel["excluded"]:
            excluded[name] = rel["excluded"]
            continue
        if rel.get("bound_by"):
            ends[name] = (rel["bound_by"]["object_type"], rel["bound_by"]["binding"])
            bound_elsewhere[name] = dict(rel["bound_by"])
            continue
        lineage = rel.get("lineage")
        if lineage is None:
            default = overrides.lineage_of(facts["source"], name)
            if default is None:
                excluded[name] = "no lineage rule for schema " + name.split(".", 1)[0]
                continue
            lineage = {"default": default}
        tid, bid = type_id(name, prefix), binding_id(name, prefix)
        taken: set[str] = set()
        properties = []
        for column in rel["columns"]:
            pname = property_name(column["name"], taken)
            prop_of[(name, column["name"])] = pname
            prop: dict[str, Any] = {"name": pname, "value_type": column["value_type"]}
            if column.get("description"):
                prop["description"] = column["description"]
            if column.get("enum_values"):
                prop["enum_values"] = list(column["enum_values"])
            prop["sources"] = [{"binding": bid, "path": column["name"],
                                "parser": PARSERS[column["value_type"]]}]
            properties.append(prop)
        verified_key = rel["key_source"] in identity["verified_key_sources"]
        key_evidence = rel["key_evidence"]
        types[tid] = {
            "id": tid,
            "description": rel["description"],
            "availability": {"state": "served"},
            "bindings": [{
                "id": bid, "source": facts["source"], "relation": name, "key": list(rel["key"]),
                "lineage": dict(lineage),
                "evidence": rel["evidence"] + [key_evidence],
                "note": rel.get("note") or overrides.lineage_note,
            }],
            "properties": properties,
            "identity_keys": [{
                "name": identity["key_name"],
                "properties": [prop_of[(name, c)] for c in rel["key"]],
                "tier": "verified" if verified_key else identity["unverified_tier"],
                "unique": verified_key, "scope": identity["scope"],
                "evidence": [key_evidence],
            }],
            "evidence": rel["evidence"],
        }
        type_of_relation[name] = tid
        ends[name] = (tid, bid)

    by_prop = {(obj["bindings"][0]["relation"], p["sources"][0]["path"]): p
               for obj in types.values() for p in obj["properties"]}
    unit_report = _apply_units(unit_contracts, by_prop, prefix)
    links, skipped = _source_links(facts["links"], ends, type_of_relation, columns_of)
    contract_report, covered_fk = _bind_contracts(link_contracts, links, ends, columns_of)
    _unique_navigation(links, facts.get("navigation_taken", {}))
    money_report = _type_money(overrides, facts["source"], by_prop, types, links, columns_of,
                               relations, prefix)
    money_report.update(_mark_by_name(overrides, facts["source"], by_prop))

    not_served, unserved_types = _not_served(facts.get("unserved", ()),
                                             overrides.names.get(facts["source"], {}), prefix)
    for entry in skipped:
        if entry["reason"] == "target relation not served" and entry["to"] in not_served:
            entry["target_type"] = not_served[entry["to"]]["object_type"]

    fk_report = {}
    served = {name for name, rel in relations.items() if not rel["excluded"]}
    for fk_name, (column, semantic) in sorted(fm_source_fk.items()):
        if fk_name in covered_fk:
            fk_report[fk_name] = {"status": "bound", "link_type": covered_fk[fk_name]}
        else:
            fk_report[fk_name] = {
                "status": "no link contract",
                "column": column, "semantic": semantic,
                "served_relations_with_column": sorted(n for n in served if column in columns_of[n]),
                "note": "Curation lead only: FM_SOURCE_FK names the column, not the "
                        "relation pair; add a link contract or a curated link."}

    files: dict[str, dict[str, Any]] = {}
    for tid in sorted(types):
        domain = facts["file_prefix"] + snake(
            relations[types[tid]["bindings"][0]["relation"]].get("domain") or "unassigned")
        files.setdefault(domain, {"object_types": [], "link_types": []})
        files[domain]["object_types"].append(types[tid])
    domain_of_type = {t["id"]: d for d, f in files.items() for t in f["object_types"]}
    for lid in sorted(links):
        # A link from a curated type lives in the file of its generated end.
        home = domain_of_type.get(links[lid]["from"]) or domain_of_type[links[lid]["to"]]
        files[home]["link_types"].append(links[lid])
    for tid in sorted(unserved_types):
        domain = facts["file_prefix"] + snake(unserved_types[tid].pop("domain"))
        if domain in files and files[domain]["object_types"][0]["bindings"]:
            raise OverrideError(f"unserved domain {domain} is also a served domain")
        files.setdefault(domain, {"object_types": [], "link_types": []})
        files[domain]["object_types"].append(unserved_types[tid])
    docs = {f"{domain}.json": {"format": ontology_schema.FORMAT, "domain": domain,
                               "origin": "generated", "description": facts["description"],
                               "upstream_pins": dict(facts["upstream_pins"]), **files[domain]}
            for domain in sorted(files)}
    report = {
        "type_of_relation": type_of_relation, "excluded": excluded,
        "bound_elsewhere": bound_elsewhere, "not_served": not_served,
        "links_not_bound": sorted(skipped, key=lambda r: (r["from"], r["field"])),
        "link_contracts": contract_report, "fm_source_fk": fk_report,
        "unit_contracts": unit_report, "money": money_report,
        "counts": {"object_types": len(types), "link_types": len(links),
                   "properties": sum(len(t["properties"]) for t in types.values()),
                   "not_served_types": len(unserved_types)},
    }
    return docs, report


def _not_served(unserved: Any, names: Mapping[str, Mapping[str, Any]], prefix: str = ""
                ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(relation -> its declaration, not_served type id -> type) for the unserved
    relations: a curated declaration stands for its relation, any other gets a type,
    named by ``names`` (relation -> curated id, note, anchor, markings) or ``Rel*``."""
    report: dict[str, dict[str, Any]] = {}
    types: dict[str, dict[str, Any]] = {}
    gone = sorted(set(names) - {item["relation"] for item in unserved})
    if gone:
        raise OverrideError("names.json names relations the source does not declare unserved "
                            f"(served now, or a typo): {', '.join(gone)}")
    for item in sorted(unserved, key=lambda u: u["relation"]):
        relation, declared = item["relation"], item.get("declared")
        if relation in report:
            raise OverrideError(f"unserved relation {relation} declared twice by the source")
        if declared:
            report[relation] = {"object_type": declared["object_type"],
                                "upstream_name": item.get("upstream_name"),
                                "declared_by": declared["file"]}
            continue
        name = names.get(relation, {})
        tid = name.get("object_type") or type_id(relation, prefix)
        if tid in types:
            raise OverrideError(f"unserved relation {relation}: type {tid} declared twice")
        description = item["description"] + (f" Curated id {tid}: {name['note']}" if name else "")
        types[tid] = {
            "id": tid, "domain": item["domain"], "description": description[:2000],
            **({"anchor": True} if name.get("anchor") else {}),
            "availability": {"state": "not_served", "reason": item["reason"][:2000],
                             "upstream_ask": item["upstream_ask"]},
            "bindings": [], "properties": [],
            **({"markings": sorted(name["markings"])} if name.get("markings") else {}),
            "evidence": list(item["evidence"]),
        }
        report[relation] = {"object_type": tid, "upstream_name": item.get("upstream_name"),
                            "declared_by": "generated"}
        if item.get("code"):
            report[relation]["code"] = item["code"]
    return report, types


def _apply_units(unit_contracts: Mapping[str, Any],
                 by_prop: Mapping[tuple[str, str], dict[str, Any]], prefix: str = ""
                 ) -> dict[str, str]:
    """Unit and quantity-role contracts onto the properties they name (in place)."""
    report: dict[str, str] = {}
    for c in sorted(unit_contracts["contracts"], key=lambda c: c["id"]):
        p = by_prop.get((c["relation"], c["column"]))
        if p is None:
            report[c["id"]] = f"not applied: {c['relation']}.{c['column']} is not served"
        elif c.get("when"):
            # A role that holds only on some rows (e.g. one task type) cannot be a
            # property-level role: applying it would label every row.
            report[c["id"]] = ("not applied: the role holds only when "
                               f"{json.dumps(c['when'], sort_keys=True)}; curate a "
                               "conditional type or binding `where`")
        else:
            evidence = [{"kind": "workbench_code", "path": UNIT_CONTRACTS,
                         "note": f"{c['id']}: {c.get('meaning', '')}"[:2000]}]
            if c.get("role"):
                p["value_type"] = "quantity"
                p["quantity_role"] = c["role"]
                p["sources"][0]["parser"] = "decimal_text"
            if c.get("unit"):
                p["unit"] = c["unit"]
                how = "unit " + c["unit"]
            else:
                # Role-only contract: the unit is not pinned by the contract. Explicit
                # unit_unknown (C0): NULL unit + typed gap, never a placeholder unit.
                p["unit_unknown"] = {"reason": (f"Unit contract {c['id']} gives the quantity "
                                                "role only; no unit is pinned for this "
                                                "column (meaning in the evidence)."),
                                     "evidence": evidence}
                how = "unit_unknown"
            p["evidence"] = evidence
            report[c["id"]] = ("applied to " + type_id(c["relation"], prefix) + "." + p["name"] +
                               (" (role " + c["role"] + ", " if c.get("role") else " (") +
                               how + ")")
    return report


def _source_links(source_links: list[Mapping[str, Any]], ends: Mapping[str, tuple[str, str]],
                  generated: Mapping[str, str], columns_of: Mapping[str, list[str]]
                  ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """The source's foreign keys between bound relations over served columns, at least
    one end generated (``ends``: relation -> type and binding; ``generated``: the
    relations a type is generated for)."""
    links: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for fk in source_links:
        src, dst, cols, refs = fk["from"], fk["to"], fk["columns"], fk["references"]
        if src not in ends:
            continue
        missing = [c for c in cols if c not in columns_of[src]]
        name = snake(fk["field"])
        kinds = fk.get("identifier_kinds") or [None] * len(cols)
        pairs = [{"from": c, "to": r, "compare": compare,
                  **({"identifier_kind": kind} if kind else {})}
                 for c, r, compare, kind in zip(cols, refs, fk["compares"], kinds)]
        if dst not in ends:
            entry = {"from": src, "field": fk["field"], "to": dst,
                     "reason": "target relation not served"}
            if not missing and len(cols) == len(refs):
                # The link a pending binding of ``dst`` gets (ontology/schema.py
                # pending_links): its from columns are served, its target is not.
                entry["link"] = {"name": name, "cardinality": "1:1" if fk["unique"] else "N:1",
                                 "description": fk["description"], "pairs": pairs,
                                 "evidence": list(fk["evidence"])}
            skipped.append(entry)
            continue
        if src not in generated and dst not in generated:
            skipped.append({"from": src, "field": fk["field"], "to": dst,
                            "reason": "both ends curated"})
            continue
        missing += [c for c in refs if c not in columns_of[dst]]
        if missing or len(cols) != len(refs):
            skipped.append({"from": src, "field": fk["field"], "to": dst,
                            "reason": "column not served: " + ", ".join(sorted(missing))})
            continue
        (from_type, from_binding), (to_type, to_binding) = ends[src], ends[dst]
        lid = f"{from_type}.{name}"
        links[lid] = {
            "id": lid, "from": from_type, "to": to_type,
            "name": name, "reverse_name": fk["reverse"] or ("inverse_" + name)[:64],
            "cardinality": "1:1" if fk["unique"] else "N:1",
            "description": fk["description"],
            "bindings": [{
                "kind": "fk", "tier_ceiling": fk.get("tier", "verified"),
                "from_binding": from_binding, "to_binding": to_binding,
                "pairs": pairs, "evidence": list(fk["evidence"]),
            }],
        }
    return links, skipped


def _bind_contracts(link_contracts: Mapping[str, Any], links: dict[str, dict[str, Any]],
                    ends: Mapping[str, tuple[str, str]], columns_of: Mapping[str, list[str]]
                    ) -> tuple[dict[str, str], dict[str, str]]:
    """Declared link contracts: joined onto the source link over the same pairs, else a
    link of their own (in place); return the per-contract report and FM FK coverage."""
    fk_index = {(lb["from_binding"], lb["to_binding"],
                 tuple((p["from"], p["to"]) for p in lb["pairs"])): lid
                for lid, link in links.items() for lb in link["bindings"]}
    report: dict[str, str] = {}
    covered_fk: dict[str, str] = {}
    for c in sorted(link_contracts["contracts"], key=lambda c: c["id"]):
        src, dst = c["from"]["relation"], c["to"]["relation"]
        pairs = c.get("pairs") or []
        if src not in ends or dst not in ends:
            gone = [r for r in (src, dst) if r not in ends]
            report[c["id"]] = "not bound: relation not served: " + ", ".join(gone)
            continue
        missing = sorted({p["from"].split(".", 1)[0] for p in pairs} - set(columns_of[src])) + \
            sorted({p["to"].split(".", 1)[0] for p in pairs} - set(columns_of[dst]))
        if missing or not pairs or c["match"] not in ("fk", "json_path", "text_match"):
            report[c["id"]] = ("not bound: " + ("columns not served: " + ", ".join(missing)
                                                if missing else "unsupported contract"))
            continue
        if c["match"] == "text_match":
            ceiling = "lead"
        elif c.get("provenance") == "source_set_by_text_join":
            ceiling = "stored_by_text_join"
        else:
            ceiling = "verified"
        evidence = {"kind": "workbench_code", "path": LINK_CONTRACTS,
                    "note": f"link contract {c['id']}: {c.get('meaning', '')}"[:2000]}
        (from_type, from_binding), (to_type, to_binding) = ends[src], ends[dst]
        key = (from_binding, to_binding, tuple((p["from"], p["to"]) for p in pairs))
        if c["match"] == "fk" and key in fk_index:
            lid = fk_index[key]
            lb = links[lid]["bindings"][0]
            lb["link_contract"] = c["id"]
            lb["tier_ceiling"] = max(lb["tier_ceiling"], ceiling, key=TIER_ORDER.index)
            lb["evidence"].append(evidence)
            for p, cp in zip(lb["pairs"], pairs):
                p["compare"] = cp["compare"]
        else:
            name = snake(c["id"])
            lid = f"{from_type}.{name}"
            links[lid] = {
                "id": lid, "from": from_type, "to": to_type,
                "name": name, "reverse_name": ("inverse_" + name)[:64],
                "cardinality": "N:M" if c["match"] == "text_match" else "N:1",
                "description": f"Link contract {c['id']} ({c['match']}): {c.get('meaning', '')}"[:2000],
                "bindings": [{
                    "kind": c["match"], "tier_ceiling": ceiling,
                    "from_binding": from_binding, "to_binding": to_binding,
                    "pairs": [{k: p[k] for k in ("from", "to", "compare", "identifier_kind")
                               if k in p} for p in pairs],
                    "link_contract": c["id"], "evidence": [evidence],
                }],
            }
        report[c["id"]] = "bound as " + lid
        if c.get("fm_source_fk"):
            covered_fk[c["fm_source_fk"]] = lid
    return report, covered_fk


def _unique_navigation(links: dict[str, dict[str, Any]],
                       taken: Mapping[str, Any] | None = None) -> None:
    """Navigation names unique per type (name on the from side, reverse on the to side),
    also against the names curated links already use (``taken``: type -> names)."""
    taken = taken or {}
    for lid in sorted(links):
        link = links[lid]
        for end, key in (("from", "name"), ("to", "reverse_name")):
            used = {l["name"] for l in links.values() if l["from"] == link[end] and l is not link} | \
                   {l["reverse_name"] for l in links.values() if l["to"] == link[end] and l is not link} | \
                   set(taken.get(link[end], ()))
            if link[key] in used:
                base = link[key][:56] + "_" + snake(link["bindings"][0]["pairs"][0]
                                                     [end if end == "from" else "from"])[:7]
                candidate, n = base[:64], 2
                while candidate in used:
                    candidate, n = f"{base[:60]}_{n}", n + 1
                link[key] = candidate


def _type_money(overrides: Overrides, family: str,
                by_prop: Mapping[tuple[str, str], dict[str, Any]],
                types: Mapping[str, dict[str, Any]], links: Mapping[str, dict[str, Any]],
                columns_of: Mapping[str, list[str]], relations: Mapping[str, Mapping[str, Any]],
                prefix: str = "") -> dict[str, str]:
    """Type the declared money columns of source ``family`` (``money.json`` relations its
    lineage rules claim and its own ``source_columns``, in place); report per column. A
    declared relation or column the source does not have, or a column it serves as other
    than a decimal, fails generation (a typo, or a name rule skipping a column that is not
    money-typed); a declared relation the source has but does not serve now is reported.
    The currency column is the spec's ``currency_from`` (default ``currency``)."""
    report: dict[str, str] = {}
    money = overrides.money_of(family)
    for relation in sorted(money):
        unserved = relations[relation]["excluded"] if relation in relations else None
        for column, spec in sorted(money[relation].items()):
            key = f"{relation}.{column}"
            if unserved and column in columns_of[relation]:
                report[key] = f"not applied: {unserved}"
                continue
            p = by_prop.get((relation, column))
            if p is None or p["value_type"] != "decimal":
                raise OverrideError(
                    f"money.json: {key} is declared money but " +
                    (f"{family} has no such relation or column" if p is None else
                     f"{family} serves it as {p['value_type']}, not decimal") +
                    "; fix the declaration or the source")
            served = columns_of[relation]
            source = p["sources"][0]
            currency = spec.get("currency_from", "currency")
            header = spec.get("header")
            lid = f"{type_id(relation, prefix)}.{snake(header)}" if header else None
            link = links.get(lid) if lid else None
            target = types[link["to"]]["bindings"][0]["relation"] \
                if link and link["to"] in types else None
            hop = bool(link and target and link["cardinality"] in ("N:1", "1:1") and
                       link["bindings"][0]["from_binding"] == binding_id(relation, prefix))

            def read(name: str) -> str | dict[str, str] | None:
                """The record's own column ``name``, else its header's, else None."""
                if name in served:
                    return name
                return {"via_link": lid, "path": name} \
                    if hop and name in columns_of[target] else None

            notes = [f"{relation}.{column} is money ({spec['measure']}, tax basis "
                     f"{spec['tax_basis']})"]
            found = read(currency)
            if found is not None:
                source["currency_from"] = found
                notes.append(f"currency from the record's {currency} column"
                             if found == currency else
                             f"currency from its header's {currency} column ({lid})")
            else:
                source["currency_unknown"] = {"reason": (
                    f"{relation} serves no {currency} column" +
                    (f" and its header ({header}) none either" if header else "") +
                    "; no other column states the currency.")}
                notes.append("currency unknown")
            # The tax basis BC states per record (pricesIncludeTax) where the entity, or its
            # header, declares the field; else unknown, saying so.
            basis = read(spec["tax_basis_from"]) if "tax_basis_from" in spec else None
            if basis is not None:
                source["tax_basis_from"] = basis
                owner = "the record" if basis == spec["tax_basis_from"] else "its header"
                notes.append(f"tax basis from {owner}'s {spec['tax_basis_from']} (true "
                             "incl_tax, false excl_tax)")
            else:
                source["tax_basis"] = spec["tax_basis"]
                if "tax_basis_from" in spec:
                    notes.append(f"tax basis unknown: {relation} declares no "
                                 f"{spec['tax_basis_from']}" +
                                 (f" and its header ({header}) none either" if header else ""))
            if spec["measure"] == "unit_price":
                per = spec.get("per_unit_from")
                if per and per in served:
                    source["per_unit_from"] = per
                    notes.append(f"price per the unit in {per}")
                else:
                    source["per_unit_unknown"] = {"reason": (
                        f"{relation} serves no column naming the unit {column} is per.")}
                    notes.append("denominator unknown")
            p["value_type"] = "money"
            p["money_measure"] = spec["measure"]
            p["markings"] = list(overrides.money_markings)
            cite = relations[relation].get("record_evidence")
            p["evidence"] = ([dict(cite, note="; ".join(notes)[:2000])] if cite else
                             [{"kind": "workbench_code", "path": "ontology/schema/overrides/money.json",
                               "note": "; ".join(notes)[:2000]}])
            report[key] = ("money: " + "; ".join(notes[1:]) +
                           f" ({', '.join(overrides.money_markings)}-marked)")
    return report


def _mark_by_name(overrides: Overrides, family: str,
                  by_prop: Mapping[tuple[str, str], dict[str, Any]]) -> dict[str, str]:
    """Mark the columns a name rule reads (``markings.json``, in place), skipping the
    declared money columns; report per column. A rule reads the column's snake_case
    form, so a camelCase name (BC ``totalAmountIncludingTax``) reads like a snake one."""
    report: dict[str, str] = {}
    money = overrides.money_of(family)
    for rule in overrides.name_rules:
        for (relation, column), p in sorted(by_prop.items()):
            if p["value_type"] not in rule.value_types or \
                    column in money.get(relation, {}) or not rule.reads(snake(column)):
                continue
            key = f"{relation}.{column}"
            if (relation, column) in rule.exceptions:
                report[key] = f"{rule.exception_label}: {rule.exceptions[(relation, column)]}"
                continue
            p["markings"] = sorted(set(p.get("markings", ())) | {rule.marking})
            report[key] = f"{rule.marking}-marked {p['value_type']}: {rule.applied}"
    return report
