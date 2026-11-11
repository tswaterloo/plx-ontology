"""Business records linked to the staff ``Person`` who owns them: a PO's buyer, an order's
salesperson, a customer's account owner, a task's assignee. A ``post_project`` step
(``operating/build_steps.py`` ``with_owners``; steps run in name order, so after
``reviews``), link id prefix ``PREFIX``, self-contained like ``ontology.people_links``,
whose Person keys (``read_people``) it reads.

``OWNERS`` names, per record type, the owner field and how it names a person:

* ``oids`` (Planner ``assignees``): an Entra object id exactly one staff Person holds,
  ``corroborated``;
* ``local_or_address`` (Mission Control ``assignee``): an address one Person holds, or a
  bare name that is one staff Person's address local part, ``corroborated`` (the people
  links' MC rule);
* ``code`` (BC purchaser and salesperson codes, Sage salesperson codes): an address one
  Person holds is ``corroborated``; a bare code equal to one staff Person's address local
  part is a **lead** (a code is no identity: the BC salesperson table with its e-mail is
  not captured);
* ``name`` (FileMaker ``requisitioner_c``): a person's name spelled as one staff Person's
  local part (``first.last``, ``firstlast``, ``flast``, ``first_last``, or a one-word
  name), a **lead**; spellings naming two Persons name nobody.

The Portal ``account_manager_id`` / PO ``created_by`` name an ``app_user`` whose address
the reporting API does not serve, so they link nobody yet.

Excluded people: there is no Person for an excluded person (``people_directory``), and
this step also drops every Person whose object id or address the exclusion
(``PLX_EXCLUDED_PEOPLE_OIDS`` / ``_FILE``, ``integrations.people_exclusion``) names. With
no usable exclusion list the step writes nothing (counted ``exclusion_unavailable``):
fail closed. A pair a person rejected (``contradicted``) is never linked.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter
import json
import os
import re
from typing import Any, Iterable, Mapping

from ontology import mentions
from ontology.people_links import Holder, People, _json, keys, read_people
from ontology.sender_parties import _digest, _q, _values, _versions, own_domains

PREFIX = "owner:"
_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)
BATCH = 5000
FIRM_KEYS = ("oid", "address")

# record type -> owner property -> (link type, how the value names a person)
OWNERS: dict[str, dict[str, tuple[str, str]]] = {
    "PurchaseOrder": {"purchaser_code": ("PurchaseOrder.buyer", "code"),
                      "requisitioner": ("PurchaseOrder.buyer", "name")},
    "SalesOrder": {"salesperson_code": ("SalesOrder.sales_rep", "code")},
    "Customer": {"salesperson_code": ("Customer.account_owner", "code")},
    "PlannerTask": {"assignees": ("PlannerTask.assigned_to", "oids")},
    "MCTask": {"assignee": ("MCTask.assigned_to", "local_or_address")},
}
LINK_TYPES = tuple(sorted({lt for props in OWNERS.values() for lt, _h in props.values()}))
_WORD = re.compile(r"[a-z]+")


def owner_keys(how: str, value: Any) -> list[list[tuple[str, str]]]:
    """The key spellings of one stored owner value: a list of alternatives, each a list of
    (key kind, key). One value names a person only when all its spellings agree."""
    if how in ("oids", "local_or_address"):
        return [[k] for k in keys(how, value)]
    if not isinstance(value, str) or not value.strip():
        return []
    if how == "code":
        if "@" in value:
            return [[k] for k in keys("address", value)]
        return [[k] for k in keys("local", value)]
    if how == "name":
        words = _WORD.findall(value.strip().lower())
        if not words or len(words) > 3:
            return []
        if len(words) == 1:
            return [[("local", words[0])]] if len(words[0]) >= 3 else []
        first, last = words[0], words[-1]
        return [[("local", s) for s in dict.fromkeys(
            (f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}", f"{first}_{last}"))]]
    return []


def _excluder() -> Any:
    """The people exclusion from the environment, or None when it is unavailable."""
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(os.environ))
    return None if guard.unavailable else guard


def _drop_excluded(conn: Any, people: People, guard: Any, counts: Counter) -> None:
    """Remove from ``people`` every Person whose object id or address ``guard`` excludes."""
    excluded: set[str] = set()
    for row, prop in _values(conn, ("object_id", "addresses"), ("Person:",)):
        listed = _json(row["value"]) if prop == "addresses" else [row["value"]]
        for value in listed if isinstance(listed, list) else []:
            if isinstance(value, str) and guard.decides(value) and guard.is_excluded(value):
                excluded.add(row["object_id"])
    for table in people.by.values():
        for key in list(table):
            for person in excluded & set(table[key]):
                del table[key][person]
            if not table[key]:
                del table[key]
    counts["persons_excluded"] += len(excluded)


def _judge(people: People, spellings: list[list[tuple[str, str]]]
           ) -> list[tuple[str, Holder, str]]:
    """(outcome, holder, key kind) per alternative of one value."""
    out = []
    for alternative in spellings:
        found: dict[str, tuple[Holder, str]] = {}
        ambiguous = False
        for kind, key in alternative:
            outcome, holder = people.judge(kind, key)
            if outcome == "ambiguous":
                ambiguous = True
            elif holder is not None:
                found.setdefault(holder.person, (holder, kind))
        if ambiguous or len(found) > 1:
            out.append(("ambiguous", None, ""))
        elif found:
            holder, kind = next(iter(found.values()))
            out.append(("linked", holder, kind))
        else:
            out.append(("no_person", None, ""))
    return out


def declared_owners(schema_links: Mapping[str, Mapping[str, Any]]
                    ) -> dict[str, dict[str, tuple[str, str]]]:
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for source, props in OWNERS.items():
        for prop, (link_type, how) in props.items():
            if any(b.get("kind") == mentions.BINDING_KIND
                   for b in (schema_links.get(link_type) or {}).get("bindings", ())):
                out.setdefault(source, {})[prop] = (link_type, how)
    return out


def owner_links(conn: Any, families: Mapping[str, Mapping[str, tuple[str, str]]],
                people: People, counts: Counter) -> list[dict[str, Any]]:
    props = sorted({p for f in families.values() for p in f})
    found: list[tuple[Any, str, Holder, str, str]] = []
    for row, prop in _values(conn, props, tuple(t + ":" for t in families)):
        declared = families.get(row["object_id"].split(":", 1)[0], {}).get(prop)
        if declared is None:
            continue
        link_type, how = declared
        for outcome, holder, kind in _judge(people, owner_keys(how, row["value"])):
            counts[f"{link_type}:{outcome}"] += 1
            if holder is not None:
                tier = "corroborated" if kind in FIRM_KEYS or how == "local_or_address" \
                    else "lead"
                found.append((row, link_type, holder, prop, tier))
    versions = _versions(conn, {h.person for _r, _l, h, _p, _t in found})
    rejected = {(r[0], r[1], r[2]) for lt in LINK_TYPES for r in _q(
        conn, "SELECT from_object, to_object, link_type FROM link_rows WHERE link_type = ? "
              "AND tier = 'contradicted'", [lt])}
    out: dict[str, dict[str, Any]] = {}
    for row, link_type, holder, prop, tier in found:
        version = versions.get(holder.person)
        if version is None:
            continue
        if (row["object_id"], holder.person, link_type) in rejected:
            counts["rejected"] += 1
            continue
        link_id = PREFIX + _digest(row["object_id"], link_type, holder.person)
        held = out.get(link_id)
        if held is not None and held["tier"] == "corroborated":
            continue
        cite = {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                "digest": row["digest"], "lineage": row["lineage"]}
        reason = [{"feature": f"{prop}_key", "agrees": True, "citations": [cite]},
                  {"feature": "person_key", "agrees": True, "citations": [dict(holder.cite)]}]
        why_not = [] if tier != "lead" else [
            {"feature": "code_or_name_only", "agrees": None,
             "detail": "a code or name matched one staff address local part: a lead"}]
        out[link_id] = {
            "link_id": link_id, "link_type": link_type, "from_object": row["object_id"],
            "to_object": holder.person, "tier": tier,
            "binding_kind": mentions.BINDING_KIND, "link_contract": None,
            "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
            "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(list(dict.fromkeys(
                [row["l1_handle"], holder.handle, version["l1_handle"]])),
                separators=(",", ":")),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(row["vmarks"], row["kmarks"], holder.markings,
                                        version["markings"]),
            "recorded_at": max(row["recorded_at"] or "", holder.recorded_at or "",
                               version["recorded_at"] or ""),
            "superseded_at": None}
    return [out[k] for k in sorted(out)]


def with_owners_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``with_owners`` step (module docstring)."""
    conn = builder.connection
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?", (PREFIX, _END))
    families = declared_owners(getattr(projector, "links", {}) or {})
    counts: Counter[str] = Counter()
    if not families:
        return {"counts": {}, "link_types": {}}
    guard = _excluder()
    if guard is None:
        counts["exclusion_unavailable"] += 1
        return {"counts": dict(counts), "link_types": {}}
    people = read_people(conn, own_domains(projector))
    _drop_excluded(conn, people, guard, counts)
    rows = owner_links(conn, families, people, counts)
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts), "link_types": dict(Counter(r["link_type"] for r in rows))}


__all__ = ["LINK_TYPES", "OWNERS", "PREFIX", "owner_keys", "owner_links", "with_owners_step"]
