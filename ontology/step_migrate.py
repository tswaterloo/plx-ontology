"""Build-step changes applied in place: a step migration layer instead of a generation build.

``docs/no-builds.md`` has the design. A ``post_project`` step (``operating.build_steps``)
writes links no projection unit holds: mention, sender, review, twin and task links. Each
step owns its rows by link id prefix (:data:`STEPS`: ``sendparty:``, ``mention:``,
``docfield:`` ...), so a step's output is replaceable without touching anyone else's.

* :func:`plan` names the steps to run again when moving from the code at a served commit
  to this tree's: a step whose code files changed (:attr:`Spec.code`), or whose written
  link types changed in the schema (:attr:`Spec.writes`; then only the links starting at
  the object types of the changed link types are its to replace, ``from_types``), and the
  steps that read (``reads``) or amend (``amends``) a step run again. The schema's own
  change is the unit migration's (``ontology.schema_migrate.diff``), whose ``step_link_type``
  refusals a step migration covers; a change to the document-field derivation re-projects
  the document-field bindings there (their kept fields are re-derived from kept readings,
  never read again).
* :func:`build_layer` runs those steps with this tree's code against the served Postgres
  ontology: the rows they read (objects, versions, values, identifiers, removals, links)
  are copied out of Postgres into a working store of the generation DDL, with the unit
  migration layer laid over them (the rows its apply deletes left out, its rows added), so
  the steps see the store as it will be once the unit migration applied. Each step sees
  what a full build would show it: the other steps' links are added in name order (a
  later step's links only after the step ran) and its own are never shown to it. Captured
  documents are read through the kept readings (``build_from_state``) and only from the
  sources of the step's ``from_types``. Its links (every row in its prefixes, from its
  ``from_types``) are written to the layer with the record the apply needs.
* ``operating.pg.ontology_migrate.apply_migration(unit_layer, dsn, steps=layer)`` applies
  both in one transaction: the step's served rows deleted by prefix (and from type), the
  layer's copied in, ``store_meta.steps_digest`` switched, and the objects a delta layer
  replaced since the snapshot queued to be projected again (as the unit migration does).
  A delta layer carries its code's :func:`steps_digest`; one of older step code arriving
  after the switch applies without the step migration's objects, which are queued.

``python3 -m ontology.step_migrate plan --from-ref <served commit>`` prints the plan.

Stdlib only (psycopg for the Postgres read).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

FORMAT = "plx-step-migration/v1"
TABLE = "step_migration"
ROOT = Path(__file__).resolve().parents[1]
MENTION = "mention"


def _get(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def prefix_end(prefix: str) -> str:
    """The first text past every text starting with ``prefix`` (binary order)."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


# ------------------------------------------------------------------ what each step writes

def _profile_types() -> set[str]:
    from ontology.document_fields import profiles
    return {p["object_type"] for p in profiles()["profiles"]}


def _mention_pairs(schema: Any) -> dict[tuple[str, str], str]:
    from ontology.mentions import mention_link_types
    return mention_link_types(dict(_get(schema, "link_types") or {}))


def _writes_document_fields(schema: Any) -> set[str]:
    types = _profile_types()
    return {link for (frm, _to), link in _mention_pairs(schema).items() if frm in types}


def _writes_mentions(schema: Any) -> set[str]:
    types = _profile_types()
    return {link for (frm, _to), link in _mention_pairs(schema).items() if frm not in types}


def _named_links(*ids: str) -> Callable[[Any], set[str]]:
    def writes(schema: Any) -> set[str]:
        return set(ids) & set(_get(schema, "link_types") or {})
    return writes


def _from_type_links(from_type: str) -> Callable[[Any], set[str]]:
    def writes(schema: Any) -> set[str]:
        return {lid for lid, link in (_get(schema, "link_types") or {}).items()
                if link.get("from") == from_type and any(
                    b.get("kind") == MENTION for b in link.get("bindings", ()))}
    return writes


def _reviewed_links(schema: Any) -> set[str]:
    return {lid for lid, link in (_get(schema, "link_types") or {}).items()
            if any(b.get("kind") == "reviewed" for b in link.get("bindings", ()))}


# ------------------------------------------------------------------ what each step reads

def _types(schema: Any) -> dict[str, Any]:
    return dict(_get(schema, "object_types") or {})


def _identifier_types(schema: Any) -> set[str]:
    """Types an identifier resolves to (mentions, aliases, twins read them store-wide)."""
    return {tid for tid, t in _types(schema).items()
            if any(p.get("identifier_kind") for p in t.get("properties", ()))}


def _property_types(*names: str) -> Callable[[Any], set[str]]:
    def types(schema: Any) -> set[str]:
        return {tid for tid, t in _types(schema).items()
                if any(p.get("name") in names for p in t.get("properties", ()))}
    return types


def _ends(schema: Any, link_ids: Iterable[str]) -> set[str]:
    links = dict(_get(schema, "link_types") or {})
    return {e for lid in link_ids if lid in links
            for e in (links[lid].get("from"), links[lid].get("to")) if e}


def _stepped_link_ends(schema: Any) -> set[str]:
    """The ends of every link type a build step writes (no unit holds them)."""
    from ontology.project import _PROJECTED_LINK_KINDS
    return _ends(schema, [lid for lid, link in (_get(schema, "link_types") or {}).items()
                          if any(b.get("kind") not in _PROJECTED_LINK_KINDS
                                 for b in link.get("bindings", ()))])


def _mention_inputs(profile: bool) -> Callable[[Any], set[str]]:
    def inputs(schema: Any) -> set[str]:
        types = _profile_types()
        pairs = [p for p in _mention_pairs(schema) if (p[0] in types) == profile]
        return ({t for p in pairs for t in p} | _identifier_types(schema)
                | (types if profile else set()))
    return inputs


def _mention_froms(profile: bool) -> Callable[[Any], set[str]]:
    def froms(schema: Any) -> set[str]:
        types = _profile_types()
        return {frm for frm, _to in _mention_pairs(schema) if (frm in types) == profile}
    return froms


def _union(*parts: Callable[[Any], set[str]]) -> Callable[[Any], set[str]]:
    def inputs(schema: Any) -> set[str]:
        return set().union(*(part(schema) for part in parts))
    return inputs


def _fixed(*types: str) -> Callable[[Any], set[str]]:
    return lambda schema: set(types)


def _mention_properties() -> set[str]:
    from ontology.mentions import properties_read_by_name
    return properties_read_by_name()


def _sender_properties() -> set[str]:
    from ontology.sender_corroboration import properties_read_by_name
    return properties_read_by_name()


def _twin_inputs(schema: Any) -> set[str]:
    from ontology.identity_scoring import TWIN_LINK, TWIN_SUPPLIER_LINK
    return _ends(schema, (TWIN_LINK, TWIN_SUPPLIER_LINK)) | _identifier_types(schema)


def _every_type(schema: Any) -> None:
    """Undeclared: the step reads the whole store (any unit change runs it again)."""
    return None


def _reviews_inputs(schema: Any) -> set[str]:
    return (_ends(schema, _reviewed_links(schema)) | _stepped_link_ends(schema)
            | _identifier_types(schema))


def _party_twin_inputs(schema: Any) -> set[str]:
    from ontology.identity_scoring import PARTY_TWIN_LINKS
    return {link.split(".", 1)[0] for link in PARTY_TWIN_LINKS}


def _key_join_links(schema: Any) -> set[str]:
    from ontology.key_links import written_types
    return written_types(dict(_get(schema, "link_types") or {}))


def _key_join_inputs(schema: Any) -> set[str]:
    """The ends of every key join, the ends of the links its party paths follow and of
    their twin links (a party's records); for a confirming path, the ends of the lead's
    link type, of the links the path follows and of its twin link."""
    from ontology.key_links import confirmations, declared
    links = dict(_get(schema, "link_types") or {})
    hops: set[str] = set()
    for lid, b in declared(links):
        hops.add(lid)
        party = b["key"].get("party") or {}
        hops |= {step.lstrip("~") for side in ("from", "to") for step in party.get(side, ())}
        if party.get("twin"):
            hops.add(party["twin"])
    for lid, b in confirmations(links):
        hops |= {lid, b["confirm"]["twin"]} | {step.lstrip("~") for step in b["confirm"]["path"]}
    return _ends(schema, hops) | {lid.split(".", 1)[0] for lid in hops if "." in lid}


def _posting_links(schema: Any) -> set[str]:
    from ontology.posting_links import declared
    return set(declared(dict(_get(schema, "link_types") or {})).values())


def _posting_inputs(schema: Any) -> set[str]:
    return _ends(schema, _posting_links(schema)) | _identifier_types(schema)


def _firm_mention_links(schema: Any) -> set[str]:
    """The mention link types ``unique_mentions`` raises (every one but Mission Control's,
    whose firm links are ``task_ids``')."""
    return {link for (frm, _to), link in _mention_pairs(schema).items() if frm != "MCTask"}


def _confirmation_inputs(schema: Any) -> set[str]:
    from operating.kind_confirmation import PROJECTED_TYPE
    return {PROJECTED_TYPE} | _profile_types()


@dataclasses.dataclass(frozen=True)
class Spec:
    """One ``post_project`` step's ownership and lineage.

    ``prefixes``: the link ids it writes (and only it). ``code``: files (repository paths)
    whose change re-derives its links. ``writes``: the schema link types its links are of
    (a change of one re-derives the links from its ``from`` type). ``reads``: the steps whose
    links it reads (run again after them); ``amends``: the steps whose links it ends (run
    again after them, its own readers not). ``documents``: it reads captured documents
    (then only of the sources holding its ``from_types``). ``supported`` False: it writes
    something else than links (values, the text index): no step migration (refused).

    ``inputs``: the object types whose rows (objects, versions, values, identifiers and
    the links touching them) the step reads, under a schema; None: every type. A unit
    migration touching one of them runs the step again (:func:`step_inputs`), and the step
    build copies only these types' rows and holds every other object behind a tripwire
    that fails the build when the step reads one (:func:`guard_inputs`). ``from_inputs``:
    the types whose change re-derives only the step's links starting at them (a document
    whose text or values changed; a resolved type's change re-derives every link).

    ``properties``: the properties the step reads by name store-wide (every holder, of
    any type), named by the step's own module. Every type holding one under a schema is
    an input too (:meth:`declared`): the holders come from the schema, never a list."""
    prefixes: tuple[str, ...]
    code: tuple[str, ...]
    writes: Callable[[Any], set[str]] | None = None
    reads: tuple[str, ...] = ()
    reads_own: tuple[str, ...] = ("reviews",)
    amends: tuple[str, ...] = ()
    documents: bool = False
    supported: bool = True
    inputs: Callable[[Any], set[str] | None] = _every_type
    from_inputs: Callable[[Any], set[str]] = lambda schema: set()
    properties: Callable[[], Iterable[str]] = lambda: ()

    def declared(self, schema: Any) -> set[str] | None:
        """The object types the step reads under ``schema`` (None: every type): its
        ``inputs`` and every type holding a property it reads by name."""
        got = self.inputs(schema)
        if got is None:
            return None
        names = set(self.properties())
        return set(got) | (_property_types(*names)(schema) if names else set())


STEPS: dict[str, Spec] = {
    "confirmations": Spec((), ("operating/kind_confirmation.py",), supported=False,
                          inputs=_confirmation_inputs),
    "document_fields": Spec(("docfield:",), ("ontology/document_fields.py",
                                             "ontology/schema/document_fields.json"),
                            writes=_writes_document_fields, inputs=_mention_inputs(True),
                            from_inputs=_mention_froms(True)),
    "mentions": Spec(("mention:",), ("ontology/mentions.py", "ontology/finish_parallel.py",
                                   "ontology/list_documents.py"),
                     writes=_writes_mentions, documents=True, inputs=_mention_inputs(False),
                     from_inputs=_mention_froms(False), properties=_mention_properties),
    "key_joins": Spec(("keyjoin:",), ("ontology/key_links.py", "ontology/posting_links.py",
                                      "ontology/identity_scoring.py"),
                      writes=_key_join_links, inputs=_key_join_inputs),
    "reviews": Spec(("rvw-",), ("ontology/reviews.py",), writes=_reviewed_links,
                    amends=("document_fields", "mentions"), inputs=_reviews_inputs),
    "roles": Spec(("roles:",), ("ontology/party_roles.py",),
                  writes=_named_links("Customer.same_party_as_supplier"),
                  inputs=_fixed("Customer", "Supplier")),
    "sender_corroboration": Spec(("docfirm:", "doccarrier:"),
                                 ("ontology/sender_corroboration.py",
                                  "ontology/sender_parallel.py"),
                                 reads=("document_fields",), documents=True,
                                 inputs=_union(_stepped_link_ends, _identifier_types,
                                               _twin_inputs),
                                 properties=_sender_properties),
    "sender_parties": Spec(("sendparty:",), ("ontology/sender_parties.py",),
                           writes=_named_links("Email.sent_by_supplier",
                                               "Email.sent_by_customer",
                                               "CalendarEvent.with_supplier",
                                               "CalendarEvent.with_customer",
                                               "Meeting.with_supplier", "Meeting.with_customer"),
                           inputs=_fixed("Email", "CalendarEvent", "Meeting", "Customer",
                                         "Supplier", "Contact")),
    "with_people": Spec(("person:",), ("ontology/people_links.py", "ontology/sender_parties.py",
                                 "ontology/mentions.py", "ontology/document_fields.py",
                                 "ontology/schema/document_fields.json"),
                        writes=_named_links("Email.sent_by_person", "Email.addressed_to_person",
                                            "CalendarEvent.with_person", "Meeting.with_person",
                                            "TeamsChatMessage.sent_by_person",
                                            "TeamsChannelMessage.sent_by_person",
                                            "PlannerTask.with_person", "MCTask.with_person",
                                            "MCApproval.with_person"),
                        inputs=_fixed("Person", "Email", "CalendarEvent", "Meeting",
                                      "TeamsChatMessage", "TeamsChannelMessage", "PlannerTask",
                                      "MCTask", "MCApproval")),
    "task_ids": Spec(("crb-mcid:",), ("ontology/mc_task_links.py",),
                     writes=_from_type_links("MCTask"), reads=("mentions",),
                     inputs=_union(_fixed("MCTask"), _identifier_types)),
    "task_names": Spec(("mcname:",), ("ontology/mc_task_links.py",),
                       writes=_from_type_links("MCTask"),
                       inputs=_fixed("MCTask", "Customer", "Supplier", "Product")),
    # the text index's inputs are the apply's: a unit migration re-marks the text fields
    # of the objects it rewrites and refuses what it cannot (ontology_migrate._text_index)
    "text_index": Spec((), ("ontology/text_index.py", "ontology/text_parallel.py"),
                       supported=False, inputs=_fixed()),
    "trace_postings": Spec(("postdoc:",), ("ontology/posting_links.py",),
                           writes=_posting_links, inputs=_posting_inputs),
    "twins": Spec(("crb-po-",), ("ontology/identity_scoring.py",), inputs=_twin_inputs),
    "twins_parties": Spec(("crb-party-",), ("ontology/identity_scoring.py",),
                          reads=("twins",), inputs=_party_twin_inputs),
    "unique_mentions": Spec(("crb-mid:",), ("ontology/mention_firm.py",
                                            "ontology/mention_firm_parallel.py",
                                            "ontology/po_mention_resolve.py"),
                            writes=_firm_mention_links, reads=("mentions",),
                            # a reused order number's resolution reads the orders, their
                            # suppliers and the messages' senders (po_mention_resolve)
                            inputs=_union(_mention_inputs(False), _identifier_types,
                                          _fixed("PurchaseOrder", "Supplier", "Contact",
                                                 "Customer", "Email")),
                            from_inputs=_mention_froms(False)),
    "your_reference": Spec(("yrf-po-",), ("ontology/identity_scoring.py",),
                           reads=("twins",), inputs=_twin_inputs),
}


def step_inputs(schemas: Sequence[Any], touched: Iterable[str], present: Iterable[str],
                specs: Mapping[str, Spec] = STEPS,
                links: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """The present steps a unit migration touching the object types ``touched`` (their
    objects, versions, values or identifiers) or the links between the type pairs
    ``links`` must run again: ``{step: {"types": [...], "from": [...] | None}}``. A step's
    declared ``inputs`` (under any of ``schemas``) are the types it reads; links it reads
    are of types joining two of them (a step reading a link reads its ends). ``from`` is
    set when every touched input is one of the step's ``from_inputs`` (only its links from
    those types are re-derived)."""
    touched = set(touched)
    links = set(links)
    out: dict[str, Any] = {}
    if not touched and not links:
        return out
    for name in sorted(set(present) & set(specs)):
        spec = specs[name]
        declared: set[str] | None = set()
        froms: set[str] = set()
        for schema in schemas:
            got = spec.declared(schema)
            declared = None if got is None or declared is None else declared | set(got)
            froms |= set(spec.from_inputs(schema))
        reached = {t for pair in links for t in pair
                   if declared is None or set(pair) <= declared}
        hit = sorted(touched | reached) if declared is None else \
            sorted((touched & declared) | reached)
        if hit:
            out[name] = {"types": hit,
                         "from": hit if declared is not None and set(hit) <= froms else None}
    return out


def declared_inputs(run: Iterable[str], schemas: Sequence[Any],
                    specs: Mapping[str, Spec] = STEPS) -> list[str] | None:
    """The object types the steps ``run`` read (None: every type)."""
    out: set[str] = set()
    for name in run:
        for schema in schemas:
            got = specs[name].declared(schema)
            if got is None:
                return None
            out |= set(got)
    return sorted(out)


def step_of(link_id: str, specs: Mapping[str, Spec] = STEPS) -> str | None:
    """The step owning ``link_id`` (None: a projected link)."""
    for name, spec in specs.items():
        if any(link_id.startswith(p) for p in spec.prefixes):
            return name
    return None


def _read(path: str, ref: str | None, repo: Path) -> bytes | None:
    if ref is None:
        file = repo / path
        return file.read_bytes() if file.is_file() else None
    done = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                          capture_output=True)
    return done.stdout if done.returncode == 0 else None


def code_digests(ref: str | None = None, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> dict[str, str]:
    """step -> digest of its code files at ``ref`` (None: this tree)."""
    cache: dict[str, str] = {}
    out = {}
    for name, spec in sorted(specs.items()):
        parts = []
        for path in spec.code:
            if path not in cache:
                data = _read(path, ref, Path(repo))
                cache[path] = "absent" if data is None else hashlib.sha256(data).hexdigest()
            parts.append([path, cache[path]])
        out[name] = _sha(parts)
    return out


def steps_digest(ref: str | None = None, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> str:
    """The digest of every step's code (a delta layer records its own: ``steps_digest``)."""
    return "steps:" + _sha(code_digests(ref, repo, specs))[:32]


def _written(schema: Any, spec: Spec) -> dict[str, Any]:
    if spec.writes is None:
        return {}
    links = dict(_get(schema, "link_types") or {})
    return {lid: links[lid] for lid in sorted(spec.writes(schema)) if lid in links}


def plan_steps(served: Any, new: Any, old_code: Mapping[str, str],
               new_code: Mapping[str, str], present: Iterable[str],
               specs: Mapping[str, Spec] = STEPS,
               inputs: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The steps to run again (module docstring): ``{"run": {step: from_types | None},
    "refused": [...], "why": {step: reason}}``. ``present``: the step names this tree's
    build runs. ``inputs``: the steps whose declared inputs the unit migration touches
    (``schema_migrate.diff``'s ``steps_required``: :func:`step_inputs`)."""
    present = set(present)
    run: dict[str, Any] = {}
    why: dict[str, str] = {}
    changed: dict[str, set[str]] = {}
    refused: list[str] = []
    inputs = dict(inputs or {})
    for name in sorted(present & set(specs)):
        spec = specs[name]
        if old_code.get(name) != new_code.get(name):
            why[name] = "code"
            run[name] = None
            continue
        a, b = _written(served, spec), _written(new, spec)
        if a != b:
            changed[name] = {lid for lid in set(a) | set(b) if a.get(lid) != b.get(lid)}
            why[name] = "link_types:" + ",".join(sorted(changed[name]))
            run[name] = sorted({(a.get(lid) or b.get(lid))["from"] for lid in changed[name]})
        if name in inputs:   # what it reads changes: its links over those inputs again
            why[name] = (why[name] + ";" if name in why else "") + \
                "inputs:" + ",".join(inputs[name]["types"])
            scope = inputs[name]["from"]
            if name not in run or (run[name] is not None and scope is not None):
                run[name] = sorted(set(run.get(name) or ()) | set(scope)) \
                    if scope is not None else None
            else:
                run[name] = None
    # who reads a step run again runs again (transitively); who amends one runs again
    # (its own readers not: what it writes is the same); who reads only the links of its
    # own link types runs again when those may have changed
    grew = True
    while grew:
        grew = False
        for name in sorted(present & set(specs)):
            if name in run:
                continue
            spec = specs[name]
            hit = [d for d in spec.reads if d in run and not why[d].startswith("amends")]
            own = set(_written(new, spec)) | set(_written(served, spec))
            # a person's rejections change only with the review step's code or link types
            hit += [d for d in spec.reads_own if d in run and not why[d].startswith("amends")
                    and not why[d].startswith("inputs")
                    and ((run[d] is None and why[d] == "code") or changed.get(d, set()) & own)]
            amended = [d for d in spec.amends if d in run]
            if hit or amended:
                run[name] = None
                why[name] = ("reads:" + ",".join(hit)) if hit else "amends:" + ",".join(amended)
                grew = True
    for name in sorted(run):
        if not specs[name].supported:
            refused.append(f"step_unsupported:{name}")
    return {"run": {k: run[k] for k in sorted(run)}, "why": why, "refused": refused}


def _document_field_units(new: Any) -> list[str]:
    from ontology.project_incremental import plan as unit_plan
    the_plan = unit_plan(new)
    bindings = {b["id"]: b for t in (_get(new, "object_types") or {}).values()
                for b in t.get("bindings", ())}
    return sorted(u for u, row in the_plan.rows.items() if row[0] == "binding"
                  and (bindings.get(row[1]) or {}).get("relation") == "documentFields")


def plan(served_ref: str, new: Any = None, repo: str | Path = ROOT,
         specs: Mapping[str, Spec] = STEPS, present: Iterable[str] | None = None
         ) -> dict[str, Any]:
    """The whole migration from the code at ``served_ref`` to this tree's
    (:func:`plan_schemas` with the served commit's schema and step code)."""
    from ontology.schema import load_schema
    from ontology import schema_migrate

    new = load_schema() if new is None else new
    served = schema_migrate.schema_at(served_ref, repo)
    old_code, new_code = code_digests(served_ref, repo, specs), code_digests(None, repo, specs)
    return plan_schemas(served, new, old_code, new_code, present, specs)


def present_steps() -> list[str]:
    """The step names this tree's build runs (``operating.build_steps.post_project``)."""
    from operating.build_steps import post_project
    return sorted(post_project(Path("/nonexistent-state"), "plx"))


def plan_schemas(served: Any, new: Any, old_code: Mapping[str, str],
                 new_code: Mapping[str, str], present: Iterable[str] | None = None,
                 specs: Mapping[str, Spec] = STEPS) -> dict[str, Any]:
    """The whole migration from ``served`` (its step code ``old_code``) to ``new``
    (``new_code``): the unit migration (``ontology.schema_migrate.diff``, its step link and
    step input refusals left to the step migration, the document-field bindings forced
    when their derivation changed) and the step migration (:func:`plan_steps`: the steps
    whose code, written link types or declared inputs changed, and their readers)."""
    from ontology import schema_migrate

    present = sorted(present_steps() if present is None else present)
    first = plan_steps(served, new, old_code, new_code, present, specs)
    force = []
    if "document_fields" in first["run"] and first["why"]["document_fields"] == "code":
        force = _document_field_units(new)
    units = schema_migrate.diff(served, new, force=force, present=present, specs=specs)
    steps = plan_steps(served, new, old_code, new_code, present, specs,
                       inputs=units.get("steps_required"))
    covered = set()
    for r in units["refused"]:
        if r.startswith("step_link_type:"):
            lid = r.split(":")[1]
            if any(lid in _written(s, specs[n]) for n in steps["run"] for s in (served, new)):
                covered.add(r)
        elif r.startswith("step_input_changed:") and r.split(":")[1] in steps["run"]:
            covered.add(r)
    units["refused"] = [r for r in units["refused"] if r not in covered]
    out = {"format": FORMAT, "units": units, "steps": steps,
           "steps_from": "steps:" + _sha(old_code)[:32],
           "steps_to": "steps:" + _sha(new_code)[:32]}
    out["id"] = _sha([units["id"], out["steps_from"], out["steps_to"],
                      steps["run"]])[:16]
    return out


# ------------------------------------------------------------------ the served store, copied

def _owned(name: str, scope: Any, specs: Mapping[str, Spec]) -> list[dict[str, Any]]:
    return [{"step": name, "prefix": p, "end": prefix_end(p),
             "from": None if scope is None else [f"{t}:" for t in scope]}
            for p in specs[name].prefixes]


def _is_owned(link_id: str, from_object: str, owned: Sequence[Mapping[str, Any]]) -> bool:
    for o in owned:
        if link_id.startswith(o["prefix"]) and (
                o["from"] is None or from_object.startswith(tuple(o["from"]))):
            return True
    return False


def _unit_drop(record: Mapping[str, Any] | None) -> tuple[list[str], list[str],
                                                           list[tuple[str, ...]]]:
    """(types, prefixes, link units) the unit migration's apply deletes."""
    if not record:
        return [], [], []
    bindings = record["delete"]["bindings"].values()
    return ([b["type"] for b in bindings], [b["prefix"] for b in bindings],
            [(x["link_type"], x["kind"], x["from"], x["to"]) for x in record["delete"]["links"]])


def _rows(cur: Any, sql: str, params: Sequence[Any] = (), size: int = 20_000
          ) -> Iterator[list[dict[str, Any]]]:
    cur.execute(sql, params)
    names = [d.name for d in cur.description]
    while True:
        batch = cur.fetchmany(size)
        if not batch:
            return
        yield [dict(zip(names, r)) for r in batch]


def copy_served(dsn: str, builder: Any, *, types: Sequence[str] | None,
                unit: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Copy the served ontology's objects (of ``types``; None: every type) and their
    versions, removals, values and identifiers into ``builder``, leaving out what the unit
    migration ``unit`` (its record) deletes; yield every link touching them (the caller
    decides when each is shown to a step), in row order, the unit migration's links
    left out. Of the other objects' values, identifiers and links (none touching them)
    only the keys are copied (:data:`HELD`): the tripwire :func:`guard_inputs` lays over
    them."""
    import psycopg
    from ontology import store_ddl
    drop_types, drop_prefixes, drop_links = _unit_drop(unit)
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")   # one snapshot
        cur = pg.cursor()
        cur.execute("CREATE TEMP TABLE inp (object_id text PRIMARY KEY)")
        cur.execute(
            "INSERT INTO inp SELECT o.object_id FROM ontology.objects o WHERE "
            "(%s::text[] IS NULL OR o.object_type = ANY(%s::text[])) AND NOT EXISTS "
            "(SELECT 1 FROM unnest(%s::text[], %s::text[]) d(t, p) WHERE o.object_type = d.t "
            "AND starts_with(o.object_id, d.p))",
            (types, types, drop_types, drop_prefixes))
        cur.execute("ANALYZE inp")
        if types is not None:
            _copy_held_keys(cur, builder.connection, drop_types, drop_prefixes, drop_links)
        cols = builder._INSERT
        # every object's own row (no versions, values or identifiers outside ``types``):
        # the tripwire a step reading an undeclared type trips (:func:`guard_inputs`)
        sel = ", ".join(f"o.{c}" for c in cols["objects"])
        with pg.cursor(name="copy_objects") as named:
            for batch in _rows(named, f"SELECT {sel} FROM ontology.objects o WHERE NOT EXISTS "
                                      "(SELECT 1 FROM unnest(%s::text[], %s::text[]) d(t, p) "
                                      "WHERE o.object_type = d.t AND "
                                      "starts_with(o.object_id, d.p)) ORDER BY o.row_no",
                               (drop_types, drop_prefixes)):
                builder.add("objects", batch)
        for table, order in (("object_versions", "ver"),
                             ("object_removals", "object_id, since"),
                             ("identifiers", "object_id, identifier_kind, normalized")):
            sel = ", ".join(f"t.{c}" for c in cols[table])
            with pg.cursor(name=f"copy_{table}") as named:
                for batch in _rows(named, f"SELECT {sel} FROM ontology.{table} t JOIN inp "
                                          f"USING (object_id) ORDER BY {order}"):
                    builder.add(table, batch)
        with pg.cursor(name="copy_values") as named:
            sel = ", ".join(f"v.{c}" for c in store_ddl.VALUE_COLUMNS)
            for batch in _rows(named, f"SELECT {sel} FROM ontology.object_values v JOIN inp "
                                      "USING (object_id) ORDER BY v.object_id, v.l1_handle, "
                                      "v.binding_id, v.property"):
                builder.add("object_values", batch)
        sel = ", ".join(f"v.{c}" for c in store_ddl.LINK_COLUMNS)
        where = ("TRUE" if types is None else
                 "(l.from_object IN (SELECT object_id FROM inp) OR "
                 "l.to_object IN (SELECT object_id FROM inp))")
        with pg.cursor(name="copy_links") as named:
            for batch in _rows(named, f"SELECT {sel} FROM ontology.link_rows l JOIN "
                                      "ontology.links v USING (link_id) WHERE " + where +
                                      " AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[], "
                                      "%s::text[], %s::text[], %s::text[]) d(t, k, f, o) "
                                      "WHERE l.link_type = d.t AND l.binding_kind = d.k AND "
                                      "starts_with(l.from_object, d.f) AND "
                                      "starts_with(l.to_object, d.o)) ORDER BY l.row_no",
                               ([x[0] for x in drop_links], [x[1] for x in drop_links],
                                [x[2] for x in drop_links], [x[3] for x in drop_links])):
                yield from batch


def copy_unit_layer(layer: str | Path, builder: Any) -> Iterator[dict[str, Any]]:
    """Copy the unit migration layer's rows into ``builder``; yield its links."""
    from ontology import store as S
    from ontology import store_ddl
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = builder._INSERT
        for table in ("objects", "object_versions", "object_removals", "identifiers"):
            rows = [dict(r) for r in conn.execute(
                f"SELECT {', '.join(cols[table])} FROM {table} ORDER BY rowid")]
            builder.add(table, rows)
        plain = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
        try:
            for batch in S._value_rows(plain):
                builder.add("object_values", [dict(zip(store_ddl.VALUE_COLUMNS, r))
                                              for r in batch])
        finally:
            plain.close()
        for r in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links "
                              "ORDER BY rowid"):
            yield dict(r)
    finally:
        conn.close()


UNDECLARED = "malformed JSON"   # what the tripwire raises (sqlite's json() on a non-JSON text)

# The keys of the rows of undeclared types' objects a step build holds back: (the view
# they join, the key table, its columns, the column the tripwire names). A read
# filtering on these columns is answered exactly; any other column trips.
HELD: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("object_values", "held_values", ("object_id", "property"), "object_id"),
    ("object_values_numbered", "held_values", ("object_id", "property"), "object_id"),
    ("identifiers", "held_identifiers",
     ("identifier_kind", "normalized", "object_id", "property"), "object_id"),
    ("links", "held_links", ("link_id", "link_type", "from_object", "to_object", "tier",
                             "binding_kind", "superseded_at"), "link_id"),
)
_HELD_INDEXES = {"held_values": (("property",), ("object_id",)),
                 "held_identifiers": (("normalized",), ("object_id",), ("identifier_kind",)),
                 "held_links": (("link_id",), ("from_object",), ("to_object",),
                                ("link_type",))}


def _held_tables(conn: sqlite3.Connection) -> None:
    for _view, table, cols, _key in HELD:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    for table, indexes in _HELD_INDEXES.items():
        for cols in indexes:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_{'_'.join(cols)} ON "
                         f"{table}({', '.join(cols)})")


def _copy_held_keys(cur: Any, conn: sqlite3.Connection, drop_types: Sequence[str],
                    drop_prefixes: Sequence[str], drop_links: Sequence[Sequence[str]]) -> None:
    """Copy into ``conn`` the keys of the served values, identifiers and links of the
    objects outside the temporary ``inp`` (the declared types' objects), leaving out
    what the unit migration deletes."""
    _held_tables(conn)
    cur.execute("CREATE TEMP TABLE outp (object_id text PRIMARY KEY)")
    cur.execute(
        "INSERT INTO outp SELECT o.object_id FROM ontology.objects o WHERE NOT EXISTS "
        "(SELECT 1 FROM inp WHERE inp.object_id = o.object_id) AND NOT EXISTS "
        "(SELECT 1 FROM unnest(%s::text[], %s::text[]) d(t, p) WHERE o.object_type = d.t "
        "AND starts_with(o.object_id, d.p))", (drop_types, drop_prefixes))
    cur.execute("ANALYZE outp")
    queries = {
        "held_values": ("SELECT DISTINCT v.object_id, v.property FROM ontology.object_values "
                        "v JOIN outp USING (object_id)", ()),
        "held_identifiers": ("SELECT DISTINCT t.identifier_kind, t.normalized, t.object_id, "
                             "t.property FROM ontology.identifiers t JOIN outp "
                             "USING (object_id)", ()),
        "held_links": (
            "SELECT v.link_id, v.link_type, v.from_object, v.to_object, v.tier, "
            "v.binding_kind, v.superseded_at FROM ontology.link_rows l JOIN ontology.links v "
            "USING (link_id) WHERE NOT EXISTS (SELECT 1 FROM inp WHERE inp.object_id = "
            "l.from_object) AND NOT EXISTS (SELECT 1 FROM inp WHERE inp.object_id = "
            "l.to_object) AND NOT EXISTS (SELECT 1 FROM "
            "unnest(%s::text[], %s::text[], %s::text[], %s::text[]) d(t, k, f, o) WHERE "
            "l.link_type = d.t AND l.binding_kind = d.k AND starts_with(l.from_object, d.f) "
            "AND starts_with(l.to_object, d.o))",
            tuple([x[i] for x in drop_links] for i in range(4))),
    }
    for table, (sql, params) in queries.items():
        cols = next(c for _v, t, c, _k in HELD if t == table)
        with cur.connection.cursor(name=f"held_{table}") as named:
            for batch in _rows(named, sql, params):
                conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})",
                                 [tuple(r[c] for c in cols) for r in batch])


class UndeclaredRead(RuntimeError):
    """A re-run step read an object type its ``Spec.inputs`` do not declare."""


def guard_inputs(conn: sqlite3.Connection, types: Sequence[str] | None,
                 step_prefixes: Iterable[str] = ()) -> None:
    """Hold the working store's rows outside ``types`` behind a tripwire that raises when
    a statement reaches one (every connection to the file sees it, the steps' worker
    processes too). ``objects`` becomes a view of the copied rows that raises at an
    object of another type (by type, by id or by scan). ``object_values``, ``identifiers``
    and ``links`` become their rows of the declared types' objects joined with the held
    keys (:data:`HELD`) of the other objects' rows: a read by property name, identifier,
    object or link type reaching one raises (a domain's holders of an undeclared type
    are never silently left out), and a read the keys answer as nothing passes. Rows of
    other types already here (the unit layer's) are held the same way. Links of
    ``step_prefixes`` (the build steps' own, derived again rather than read) are never
    held. None: nothing is held."""
    if types is None:
        return
    allowed = ", ".join("'" + t.replace("'", "''") + "'" for t in sorted(types)) or "NULL"
    conn.commit()
    _held_tables(conn)
    other = f"SELECT object_id FROM objects WHERE object_type NOT IN ({allowed})"
    conn.execute("INSERT INTO held_values SELECT DISTINCT object_id, property FROM "
                 f"object_values WHERE object_id IN ({other})")
    conn.execute(f"DELETE FROM object_values WHERE object_id IN ({other})")
    conn.execute("INSERT INTO held_identifiers SELECT identifier_kind, normalized, "
                 f"object_id, property FROM identifiers WHERE object_id IN ({other})")
    conn.execute(f"DELETE FROM identifiers WHERE object_id IN ({other})")
    keys = next(cols for view, _t, cols, _k in HELD if view == "links")
    conn.execute(f"INSERT INTO held_links SELECT {', '.join(keys)} FROM links WHERE "
                 f"from_object IN ({other}) AND to_object IN ({other})")
    conn.execute(f"DELETE FROM links WHERE from_object IN ({other}) AND to_object IN "
                 f"({other})")
    for prefix in step_prefixes:
        conn.execute("DELETE FROM held_links WHERE link_id >= ? AND link_id < ?",
                     (prefix, prefix_end(prefix)))
    conn.execute("PRAGMA legacy_alter_table = ON")   # other tables keep naming ``objects``
    conn.execute("ALTER TABLE objects RENAME TO objects_held")
    conn.execute("ALTER TABLE identifiers RENAME TO identifiers_declared")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute(f"CREATE VIEW objects AS SELECT * FROM objects_held WHERE object_type IN "
                 f"({allowed}) OR json('plx-undeclared-read') IS NULL")
    for view, table, cols, key in HELD:
        if view == "identifiers":
            names = [r[1] for r in conn.execute("PRAGMA table_info(identifiers_declared)")]
            select, triggers = f"SELECT {', '.join(names)} FROM identifiers_declared", []
        else:
            names = [r[1] for r in conn.execute(f"PRAGMA table_info({view})")]
            (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND "
                                  "name = ?", (view,)).fetchone()
            select = re.split(r"\bAS\s+(?=SELECT\b)", sql, maxsplit=1, flags=re.I)[1]
            triggers = [r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (view,))]
            conn.execute(f"DROP VIEW {view}")
        trip = f"json('{{' || h.{key})"   # never JSON: raises when evaluated
        arm = ", ".join(f"h.{c}" if c in cols else trip for c in names)
        conn.execute(f"CREATE VIEW {view} AS {select} UNION ALL SELECT {arm} FROM {table} h "
                     f"WHERE (SELECT {trip}) IS NULL")   # a correlated test: checked last
        for trigger in triggers:
            conn.execute(trigger)
    conn.commit()


# ------------------------------------------------------------------ the layer

def _projector(new: Any, sources: Mapping[str, Any], normalizer: Any) -> Any:
    from ontology.project import Projector
    return Projector(new, sources, normalizer=normalizer)


def _sources_of(new: Any, sources: Mapping[str, Any], from_types: Sequence[str] | None
                ) -> dict[str, Any]:
    if from_types is None:
        return dict(sources)
    wanted = {b["source"] for t in from_types
              for b in ((_get(new, "object_types") or {}).get(t) or {}).get("bindings", ())}
    return {k: v for k, v in sources.items() if k in wanted}


def build_layer(the_plan: Mapping[str, Any], new: Any, steps: Mapping[str, Any], dsn: str,
                out: str | Path, *, sources: Mapping[str, Any] | None = None,
                unit_layer: str | Path | None = None, normalizer: Any = None,
                delta_mark: int = 0, specs: Mapping[str, Spec] = STEPS,
                sequence: int | None = None) -> dict[str, Any]:
    """Run ``the_plan``'s steps (``steps``: name -> the build's step function, this tree's
    ``operating.build_steps.post_project``) against the served ontology at ``dsn`` (with
    ``unit_layer`` laid over it) and write their links into the store ``out`` (replaced)
    with the migration record. ``sources``: the capture sources a document step reads
    (with the build's kept readings entered by the caller). Returns the report."""
    from ontology.project import open_generation
    from ontology.schema_migrate import read_layer
    from ontology.store import StoreBuilder
    from ontology import store_ddl

    run = dict(the_plan["steps"]["run"])
    if the_plan["steps"]["refused"]:
        raise ValueError("step_migration_refused:" + ",".join(the_plan["steps"]["refused"]))
    missing = sorted(set(run) - set(steps))
    if missing:
        raise ValueError("steps_missing:" + ",".join(missing))
    sources = dict(sources or {})
    unit = read_layer(unit_layer) if unit_layer is not None else None
    # the step layer is bound to the unit layer of its plan (applied together)
    units = the_plan.get("units") or {}
    rewrites = (units.get("units") or {}).get("affected") or \
        (units.get("units") or {}).get("dropped")
    if unit is None and rewrites:
        raise ValueError("unit_layer_required")
    if unit is not None and unit.get("plan") != units.get("id"):
        raise ValueError("unit_layer_of_another_plan")
    inputs = declared_inputs(run, (new,), specs)
    owned = [o for name in sorted(run) for o in _owned(name, run[name], specs)]
    order = sorted(steps)
    first = min(run) if run else None
    started = time.monotonic()
    out = Path(out)
    out.unlink(missing_ok=True)
    work = out.with_name(f".{out.name}.input")
    work.unlink(missing_ok=True)
    report: dict[str, Any] = {"id": the_plan["id"], "steps": {}, "run": run}
    held: dict[str, list[dict[str, Any]]] = {}
    shown = dropped = 0
    builder = StoreBuilder(work, foreign_keys=False)
    try:
        open_generation(builder)
        conn = builder.connection
        pending: list[dict[str, Any]] = []

        def take(rows: Iterable[dict[str, Any]]) -> None:
            nonlocal shown, dropped
            for row in rows:
                if _is_owned(row["link_id"], row["from_object"], owned):
                    dropped += 1           # the step derives it again
                    continue
                name = step_of(row["link_id"], specs)
                if name is not None and first is not None and name >= first:
                    held.setdefault(name, []).append(row)   # shown once its step's turn came
                    continue
                pending.append(row)
                shown += 1
                if len(pending) >= 20_000:
                    builder.add("links", pending)
                    pending.clear()

        take(copy_served(dsn, builder, types=inputs, unit=unit))
        if unit_layer is not None:
            take(copy_unit_layer(unit_layer, builder))
        if pending:
            builder.add("links", pending)
            pending.clear()
        report["input"] = {"links_shown": shown, "links_rederived": dropped,
                           "links_held": sum(len(v) for v in held.values()),
                           "types": inputs,
                           "seconds": round(time.monotonic() - started, 3)}
        conn.commit()
        guard_inputs(conn, inputs,   # the unit layer's other types held too
                     [p for spec in specs.values() for p in spec.prefixes])
        for name in order:
            if first is None or name < first:
                continue
            if name in run:
                t = time.monotonic()
                spec = specs[name]
                srcs = _sources_of(new, sources, run[name]) if spec.documents else sources
                projector = _projector(new, srcs, normalizer)
                try:
                    result = steps[name](builder, projector)
                except sqlite3.OperationalError as exc:
                    if UNDECLARED in str(exc):   # fail closed: its inputs are wrong
                        raise UndeclaredRead(f"step_read_undeclared_type:{name}") from exc
                    raise
                except Exception as exc:
                    if UNDECLARED in str(exc) or isinstance(exc.__cause__, sqlite3.Error) \
                            and UNDECLARED in str(exc.__cause__):
                        raise UndeclaredRead(f"step_read_undeclared_type:{name}") from exc
                    raise
                conn.commit()
                report["steps"][name] = {"seconds": round(time.monotonic() - t, 3),
                                         "report": result}
                if run[name] is not None:
                    # outside its from types the served links stand (shown next): what
                    # it wrote there again is dropped, never doubled
                    froms = [f"{t}:" for t in run[name]]
                    for p in specs[name].prefixes:
                        conn.execute(
                            "DELETE FROM link_rows WHERE link_id >= ? AND link_id < ? AND "
                            "NOT (" + " OR ".join("substr(from_object, 1, ?) = ?"
                                                  for _ in froms) + ")",
                            (p, prefix_end(p),
                             *[x for f in froms for x in (len(f), f)]))
            rows = held.pop(name, [])
            for i in range(0, len(rows), 20_000):
                builder.add("links", rows[i:i + 20_000])
            conn.commit()
        # the re-derived links: every row of the steps' prefixes from their from types
        result_rows = []
        for o in owned:
            for r in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links "
                                  "WHERE link_id >= ? AND link_id < ?", (o["prefix"], o["end"])):
                row = dict(zip(store_ddl.LINK_COLUMNS, r))
                if o["from"] is None or row["from_object"].startswith(tuple(o["from"])):
                    result_rows.append(row)
    finally:
        builder.abort()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{work}{suffix}").unlink(missing_ok=True)
    layer = StoreBuilder(out, foreign_keys=False)
    try:
        open_generation(layer)
        for i in range(0, len(result_rows), 20_000):
            layer.add("links", result_rows[i:i + 20_000])
        layer.set_meta("format", FORMAT)
        layer.set_meta("steps_digest", the_plan["steps_to"])
        scope = sorted({f"{r[e].split(':', 1)[0]}:" for r in result_rows
                        for e in ("from_object", "to_object")}
                       | {p for o in owned if o["from"] for p in o["from"]})
        c = layer.connection
        from ontology.schema_migrate import content_digest
        content = content_digest(c, ("link_rows",))
        sequence = int(time.time() * 1000) if sequence is None else int(sequence)
        unit_id = unit["id"] if unit is not None else None
        record = {"id": f"{_sha([the_plan['id'], unit_id, content, sequence])[:16]}-{sequence}",
                  "plan": the_plan["id"], "unit": unit_id, "content": content,
                  "sequence": sequence, "from": the_plan["steps_from"],
                  "to": the_plan["steps_to"], "owned": owned, "scope": scope,
                  "steps": sorted(run), "inputs": inputs, "delta_mark": int(delta_mark),
                  "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        report["id"] = record["id"]
        c.execute(f"CREATE TABLE {TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.executemany(f"INSERT INTO {TABLE} VALUES (?, ?)",
                      [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
        path = layer.keep_as_part()
    except BaseException:
        layer.abort()
        raise
    path.replace(out)
    report.update(links=len(result_rows), scope=scope,
                  seconds=round(time.monotonic() - started, 3), store=str(out))
    return report


def read_layer(path: str | Path) -> dict[str, Any]:
    """The record of a step migration layer :func:`build_layer` wrote."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {k: json.loads(v) for k, v in conn.execute(f"SELECT key, value FROM {TABLE}")}
    finally:
        conn.close()


def build_from_state(the_plan: Mapping[str, Any], new: Any, state: Path, out: Path, *,
                     dsn: str, unit_layer: Path | None = None, tenant: str = "plx",
                     scratch: Path | None = None) -> dict[str, Any]:
    """:func:`build_layer` with the host's capture sources (read-only) and the build's
    kept readings of captured files (a file is never read again when a reading is kept)."""
    import os
    import tempfile
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology import project
    from ontology.delta import _internal_domains
    from ontology.project_incremental import MEMBERS, READINGS
    from operating import build_steps
    from operating.host import state_captures
    from operating.pg.ontology_migrate import delta_mark

    mark = delta_mark(dsn)   # before the snapshot: anything later is queued at the apply
    projection = state / "projection"
    with contextlib.ExitStack() as stack:
        sources = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(projection / READINGS))
        stack.enter_context(kept_members(projection / MEMBERS, blobs))
        tmp = scratch or Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix=".step-migrate-", dir=out.parent)))
        steps = build_steps.post_project(state, tenant, scratch=tmp)
        from ontology.schema_migrate import next_sequence
        return build_layer(the_plan, new, steps, dsn, out, sources=sources,
                           unit_layer=unit_layer, delta_mark=mark,
                           sequence=next_sequence(dsn))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.step_migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="the unit and step migration from a commit's code")
    p.add_argument("--from-ref", required=True)
    b = sub.add_parser("build", help="the step migration layer over the served ontology")
    b.add_argument("--from-ref", required=True)
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--unit-layer", default=None,
                   help="the unit migration layer applied in the same transaction")
    b.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    the_plan = plan(args.from_ref)
    if args.command == "plan":
        print(json.dumps(the_plan, indent=1, sort_keys=True, default=str))
        return 1 if the_plan["units"]["refused"] or the_plan["steps"]["refused"] else 0
    from ontology.schema import load_schema
    from operating.pg import config
    report = build_from_state(the_plan, load_schema(), Path(args.state_dir), Path(args.out),
                              dsn=args.dsn or config.dsn(),
                              unit_layer=Path(args.unit_layer) if args.unit_layer else None)
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
