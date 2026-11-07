"""A customer linked to the supplier record of the same party in the same system
(``Customer.same_party_as_supplier``): a company PLX both sells to and buys from.

One ``post_project`` step (``operating/build_steps.py`` ``roles``; steps run in name
order, so after ``reviews``), link id prefix ``PREFIX``, self-contained: it reads the
current store only, removes the rows it wrote before and writes them again, so it runs in
a full build or alone on a store in place (the step migration).

* Inputs: the current ``name`` of every ``Customer`` and ``Supplier``; each record's binding
  (its source and lineage: one system) and audience scope (a BC company); the own-party
  names (``ontology/schema/document_fields.json`` ``own_parties``); a person's rejections
  (``contradicted`` links of a pair).
* A **lead** joins a customer and a supplier of one system (same source and lineage, same
  audience scope: one BC company, one FileMaker file, the Portal's own tables) whose names
  are equal under the general resolver's name rules
  (``ontology.identity_scoring.normalize_name``: case, punctuation, legal words aside),
  when the name is no own party's, has at least ``MIN_LETTERS`` letters, and names at most
  ``MAX_RECORDS`` records of each role there (a chain's many accounts are no one party).
  A name is no identifier, so the link is a lead, never firm; twins in other systems are
  reached through their ``same_record_as`` links.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from ontology import mentions
from ontology.identity_scoring import normalize_name
from ontology.mc_task_links import current_values, own_words

PREFIX = "roles:"
LINK_TYPE = "Customer.same_party_as_supplier"
LEAD = "lead"
BATCH = 2000
MIN_LETTERS = 4
MAX_RECORDS = 3


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def _system(bindings: Mapping[str, Any], binding_id: str) -> tuple[str, str] | None:
    entry = bindings.get(binding_id)
    if entry is None:
        return None
    _tid, b = entry
    lineage = b.get("lineage")
    lineage = lineage.get("default") if isinstance(lineage, Mapping) else lineage
    return (str(b.get("source")), str(lineage))


def _declared(links: Mapping[str, Any]) -> bool:
    return any(b.get("kind") == mentions.BINDING_KIND
               for b in (links.get(LINK_TYPE) or {}).get("bindings", ()))


def role_links(conn: Any, bindings: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Counter]:
    """The lead rows (module docstring) and the outcome counts."""
    counts: Counter[str] = Counter()
    own = own_words()
    groups: dict[tuple, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    oids = []
    rows = list(current_values(conn, ("name",), ("Customer", "Supplier")))
    scopes = {}
    for i in range(0, len(rows), 500):
        part = sorted({r["object_id"] for r in rows[i:i + 500]})
        for r in _q(conn, "SELECT object_id, binding_id, audience_key FROM objects WHERE "
                          f"object_id IN ({','.join('?' * len(part))})", part):
            scopes[r["object_id"]] = (r["binding_id"], r["audience_key"].rsplit(":", 1)[0])
    for row in rows:
        oid = row["object_id"]
        role = oid.split(":", 1)[0]
        name = normalize_name(row["value"])
        if not name or sum(ch.isalpha() for ch in name) < MIN_LETTERS:
            continue
        if set(name.split()) & own:
            counts["own_party"] += 1
            continue
        binding, scope = scopes.get(oid, (None, None))
        system = _system(bindings, binding) if binding else None
        if system is None:
            continue
        groups[(system, scope, name)][role].append(row)
        oids.append(oid)
    out: dict[str, dict[str, Any]] = {}
    rejected: dict[str, set[tuple[str, str]]] = {}
    for (_system_, _scope, name), roles in sorted(groups.items()):
        customers = {r["object_id"]: r for r in roles.get("Customer", ())}
        suppliers = {r["object_id"]: r for r in roles.get("Supplier", ())}
        if not customers or not suppliers:
            continue
        if len(customers) > MAX_RECORDS or len(suppliers) > MAX_RECORDS:
            counts["many_records"] += 1
            continue
        counts["pairs"] += 1
        for c_oid, c_row in sorted(customers.items()):
            if c_oid not in rejected:
                rejected[c_oid] = {(r[0], r[1]) for r in _q(
                    conn, "SELECT to_object, link_type FROM link_rows WHERE from_object = ? "
                          "AND tier = 'contradicted'", [c_oid])}
            for s_oid, s_row in sorted(suppliers.items()):
                if (s_oid, LINK_TYPE) in rejected[c_oid]:
                    counts["rejected"] += 1
                    continue
                link_id = PREFIX + _digest(c_oid, s_oid)
                reason = [{"feature": "party_name", "agrees": True, "level": "exact",
                           "normalized": name,
                           "citations": [{"l1_handle": c_row["l1_handle"],
                                          "field_path": c_row["field_path"]},
                                         {"l1_handle": s_row["l1_handle"],
                                          "field_path": s_row["field_path"]}]},
                          {"feature": "same_system", "agrees": True}]
                why_not = [{"feature": "name_only", "agrees": None,
                            "detail": "the records share a name only: a lead"}]
                out[link_id] = {
                    "link_id": link_id, "link_type": LINK_TYPE, "from_object": c_oid,
                    "to_object": s_oid, "tier": LEAD, "binding_kind": mentions.BINDING_KIND,
                    "link_contract": None, "reason": _canon(reason), "why_not": _canon(why_not),
                    "evidence_handles": _canon(list(dict.fromkeys([c_row["l1_handle"],
                                                                   s_row["l1_handle"]]))),
                    "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
                    "markings": mentions._marks(c_row["vmarks"], c_row["kmarks"],
                                                s_row["vmarks"], s_row["kmarks"]),
                    "recorded_at": max(c_row["recorded_at"], s_row["recorded_at"]),
                    "superseded_at": None}
    counts["links"] = len(out)
    return [out[k] for k in sorted(out)], counts


def roles_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``roles`` step (module docstring)."""
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    if _declared(getattr(projector, "links", {}) or {}):
        rows, counts = role_links(builder.connection, getattr(projector, "bindings", {}) or {})
    builder.connection.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                               (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts), "link_tiers": {LEAD: len(rows)} if rows else {}}


__all__ = ["LINK_TYPE", "PREFIX", "role_links", "roles_step"]
