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
import ast
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
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
    something else than links or owned values (the text index): no step migration
    (refused).

    ``values``: the ``post_project`` values it writes, as (object type, binding) pairs: it
    owns every value of parser ``post_project`` of that binding on that type's objects
    (the ``confirmations`` step's confirmed document kinds). The step build drops the
    served ones before the steps run, the layer holds the re-derived ones (with their
    objects' own and version rows, which the apply never copies) and the apply replaces
    the served ones by them (``ontology_migrate._apply_step_values``).

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
    values: tuple[tuple[str, str], ...] = ()
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
    # kind_confirmation.PROJECTED_TYPE / PROJECTED_BINDING (a test holds them equal)
    "confirmations": Spec((), ("operating/kind_confirmation.py",),
                          values=(("SharePointFile", "sharepoint.drive_items"),),
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
                           # the records a message names decide its sender's side
                           reads=("document_fields", "mentions", "key_joins"),
                           inputs=_fixed("Email", "CalendarEvent", "Meeting", "Customer",
                                         "Supplier", "Contact", "SalesOrder", "PurchaseOrder",
                                         "SalesInvoice", "PurchaseInvoice", "Shipment",
                                         "AgedPayable", "CustomerPurchaseOrder")),
    "with_owners": Spec(("owner:",), ("ontology/owner_links.py", "ontology/people_links.py",
                                      "ontology/sender_parties.py", "ontology/mentions.py",
                                      "integrations/people_exclusion.py"),
                        writes=_named_links("PurchaseOrder.buyer", "SalesOrder.sales_rep",
                                            "Customer.account_owner", "PlannerTask.assigned_to",
                                            "MCTask.assigned_to"),
                        # its contradicted MCTask.assigned_to links: a type the task id
                        # and name steps write too (every MCTask link type)
                        reads=("task_ids", "task_names"),
                        inputs=_fixed("Person", "PurchaseOrder", "SalesOrder", "Customer",
                                      "PlannerTask", "MCTask")),
    "with_people": Spec(("person:",), ("ontology/people_links.py", "ontology/sender_parties.py",
                                 "ontology/mentions.py", "ontology/document_fields.py",
                                 "ontology/schema/document_fields.json"),
                        writes=_named_links("Email.sent_by_person", "Email.addressed_to_person",
                                            "CalendarEvent.with_person", "Meeting.with_person",
                                            "TeamsChatMessage.sent_by_person",
                                            "TeamsChannelMessage.sent_by_person",
                                            "TeamsChat.with_person",
                                            "TeamsChannel.with_person",
                                            "PlannerTask.with_person", "MCTask.with_person",
                                            "MCApproval.with_person"),
                        inputs=_fixed("Person", "Email", "CalendarEvent", "Meeting",
                                      "TeamsChatMessage", "TeamsChannelMessage", "TeamsChat",
                                      "TeamsChannel", "PlannerTask", "MCTask", "MCApproval")),
    # demand pegging reads open order and PO lines, their orders, items and assembly orders
    "supply_pegging": Spec(("peg:",), ("ontology/demand_pegging.py",
                                       "ontology/schema/pegging.json",
                                       "ontology/sender_parties.py", "ontology/mentions.py"),
                           writes=_named_links("SalesOrderLine.supplied_by",
                                               "SalesOrder.awaits_purchase_order"),
                           inputs=_fixed("SalesOrder", "SalesOrderLine", "PurchaseOrder",
                                         "PurchaseOrderLine", "Product", "AssemblyOrder",
                                         "AssemblyOrderAllocation", "AssemblyOrderComponent")),
    "task_ids": Spec(("crb-mcid:",), ("ontology/mc_task_links.py",),
                     writes=_from_type_links("MCTask"), reads=("mentions",),
                     inputs=_union(_fixed("MCTask"), _identifier_types)),
    "task_names": Spec(("mcname:",), ("ontology/mc_task_links.py",),
                       writes=_from_type_links("MCTask"),
                       inputs=_fixed("MCTask", "Customer", "Supplier", "Product")),
    # the text index's inputs are the apply's: a unit migration re-marks the text fields
    # of the objects it rewrites and refuses what it cannot (ontology_migrate._text_index);
    # a change of its code is refused unless it only marks (MARKS_ONLY: re-marked in place)
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
                            # po_mention_resolve reads the order and party twins
                            writes=_firm_mention_links,
                            reads=("mentions", "twins", "twins_parties"),
                            # a reused order number's resolution reads the orders, their
                            # suppliers and the messages' senders (po_mention_resolve)
                            inputs=_union(_mention_inputs(False), _identifier_types,
                                          _fixed("PurchaseOrder", "Supplier", "Contact",
                                                 "Customer", "Email")),
                            from_inputs=_mention_froms(False)),
    "your_reference": Spec(("yrf-po-",), ("ontology/identity_scoring.py",),
                           reads=("twins",), inputs=_twin_inputs),
}


# Code changes of an unsupported step that change only the markings its rows carry, as
# (from, to) pairs of its code digest (:func:`code_digests`), each pinned by reading the
# change: a step migration across one re-marks the step's served rows in place (no text is
# read again, no row written but its markings) instead of being refused. Any other change
# of such a step's code is refused as before (an unpinned pair).
# text_index, 7aba9f1245: the text fields of a personal financial or HR-confidential
# document (``text_index._sensitive``: a pay stub, an offer or termination letter, a payroll
# or HR confidentiality label) carry ``finance`` and ``pii`` too (:func:`text_marks`).
MARKS_ONLY: dict[str, frozenset[tuple[str, str]]] = {
    "text_index": frozenset({
        ("becab53876388be8bf17db03631e2af2ecbd9c89eeb814e3b715dc6c0d6c403f",
         "134dfec62ca16d17b649eed64dabcd8b1ad3db3c740c9fd1037831b5f99e801b")}),
}
# Code changes of a step proven to write the same rows, as (from, to) pairs of its code
# digest (:func:`code_digests`), each pinned by reading the change: a step migration across
# one does not run the step again for its code (its link types and inputs still decide),
# and the served step code still switches to the new digest. Any other pair is a change.
# c7035f86f1 (main, 11-09): the SQLite DDL of a module's own side store moved verbatim into
# ``operating/declared_schema`` (version 1 is the earlier DDL, idempotent): document_fields'
# kept-fields memo (``KeptFields``) and kind_confirmation's confirmation store. Neither
# step's rows read or depend on that DDL. From the build of 11-10 (fb77cd38eb's step code) to the tree that moved it.
SAME_ROWS: dict[str, frozenset[tuple[str, str]]] = {
    "document_fields": frozenset({
        ("ab0f767f19df54ad663aae34db67584c2ee1d25230104edea7735af56e68eb38",
         "b4256f70d305a06e1a7ee37bb9068ddb6f39bfe6fe720fe9342bc016d8ff1de2")}),
    "confirmations": frozenset({
        ("fff623a24d1b3f3fb7ed8ba05247e25092474886446e5ceb6a3e35f3201d3efd",
         "3cfad85742ea1f054c91a43ccd0a56003a6ee321b079846a92b973e017e16224")}),
}
SENSITIVE_MARKS = ("finance", "pii")
TEXT_MARKS = "text_marks"   # the layer's table: the version handles whose fields are marked


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


def code_at(ref: str, repo: str | Path = ROOT) -> dict[str, str] | None:
    """step -> digest of its code files at ``ref``, as that commit's own ``STEPS`` lists them
    (its step names and code files, read from its ``ontology/step_migrate.py`` without
    running it): the code digests ``ref``'s build recorded, so ``steps_from`` is the digest
    the served store holds even after this tree adds a step or a code file to one. None:
    ``ref`` has no step migration. A ``STEPS`` entry that is not a literal ``Spec`` with a
    literal code tuple raises (never a guessed digest)."""
    data = _read("ontology/step_migrate.py", ref, Path(repo))
    if data is None:
        return None
    files: dict[str, tuple[str, ...]] | None = None
    for node in ast.parse(data).body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id == "STEPS":
            value = node.value
            if not isinstance(value, ast.Dict):
                raise ValueError(f"steps_unreadable:{ref}")
            files = {}
            for key, spec in zip(value.keys, value.values):
                if key is None or not isinstance(spec, ast.Call) or len(spec.args) < 2:
                    raise ValueError(f"steps_unreadable:{ref}")
                files[ast.literal_eval(key)] = tuple(ast.literal_eval(spec.args[1]))
    if files is None:
        return None
    specs = {name: Spec((), paths) for name, paths in files.items()}
    return code_digests(ref, repo, specs)


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
               inputs: Mapping[str, Mapping[str, Any]] | None = None,
               marks_only: Mapping[str, Iterable[tuple[str, str]]] = MARKS_ONLY,
               same_rows: Mapping[str, Iterable[tuple[str, str]]] = SAME_ROWS
               ) -> dict[str, Any]:
    """The steps to run again (module docstring): ``{"run": {step: from_types | None},
    "refused": [...], "why": {step: reason}}``, and ``"remark": [...]`` when an
    unsupported step runs again only for a code change ``marks_only`` pins (its rows are
    re-marked in place, :data:`MARKS_ONLY`); a code change ``same_rows`` pins
    (:data:`SAME_ROWS`) is no reason to run a step again. ``present``: the step names this tree's
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
        if old_code.get(name) != new_code.get(name) and \
                (old_code.get(name), new_code.get(name)) not in set(same_rows.get(name, ())):
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
    remark: list[str] = []
    for name in sorted(run):
        if specs[name].supported:
            continue
        if why.get(name) == "code" and (old_code.get(name), new_code.get(name)) in \
                set(marks_only.get(name, ())):
            remark.append(name)
        else:
            refused.append(f"step_unsupported:{name}")
    out = {"run": {k: run[k] for k in sorted(run)}, "why": why, "refused": refused}
    if remark:
        out["remark"] = remark
    return out


def _document_field_units(new: Any) -> list[str]:
    from ontology.project_incremental import plan as unit_plan
    the_plan = unit_plan(new)
    bindings = {b["id"]: b for t in (_get(new, "object_types") or {}).values()
                for b in t.get("bindings", ())}
    return sorted(u for u, row in the_plan.rows.items() if row[0] == "binding"
                  and (bindings.get(row[1]) or {}).get("relation") == "documentFields")


def served_code(path: str | Path) -> dict[str, str]:
    """The step code a build recorded (a pin file: ``{"code": {step: digest},
    "steps_digest": "steps:..."}``), for a served store built from a tree whose step code
    no commit holds (a build-only file). Refused unless the code hashes to its
    ``steps_digest``: never a guessed digest."""
    pin = json.loads(Path(path).read_text(encoding="utf-8"))
    code = pin.get("code") if isinstance(pin, dict) else None
    if not isinstance(code, dict) or not code or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in code.items()) \
            or pin.get("steps_digest") != "steps:" + _sha(code)[:32]:
        raise ValueError("served_code_unverified")
    return dict(code)


def plan(served_ref: str, new: Any = None, repo: str | Path = ROOT,
         specs: Mapping[str, Spec] = STEPS, present: Iterable[str] | None = None,
         old_code: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The whole migration from the code at ``served_ref`` to this tree's
    (:func:`plan_schemas` with the served commit's schema and step code). ``old_code``:
    the served step code when no commit holds it (:func:`served_code`); the schema still
    comes from ``served_ref``."""
    from ontology.schema import load_schema
    from ontology import schema_migrate

    new = load_schema() if new is None else new
    served = schema_migrate.schema_at(served_ref, repo)
    if old_code is None:
        old_code = code_at(served_ref, repo)
    if old_code is None:   # older than step migrations: its files under this tree's steps
        old_code = code_digests(served_ref, repo, specs)
    new_code = code_digests(None, repo, specs)
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


def _owned_values(run: Iterable[str], specs: Mapping[str, Spec]) -> list[dict[str, Any]]:
    """The values the steps ``run`` own (:attr:`Spec.values`), each with its id range."""
    return [{"step": name, "type": t, "binding": b, "prefix": f"{t}:", "end": prefix_end(f"{t}:")}
            for name in sorted(run) for t, b in specs[name].values]


VALUE_PARSER = "post_project"   # ontology.project.POST_PROJECT


def owned_values_where(owned: Sequence[Mapping[str, Any]]) -> tuple[str, list[Any]]:
    """A SQL condition (``?`` parameters) on ``object_values`` rows holding the values
    ``owned`` (:func:`_owned_values`): parser ``post_project``, the binding, the type's id
    range."""
    if not owned:
        return "FALSE", []
    where = " OR ".join("(binding_id = ? AND object_id >= ? AND object_id < ?)" for _ in owned)
    return (f"parser = ? AND ({where})",
            [VALUE_PARSER, *[x for o in owned for x in (o["binding"], o["prefix"], o["end"])]])


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


# The order values are added in (the ``object_values`` key order, then every column a
# tie could leave to the plan): one order whichever way they are copied.
_VALUE_ORDER = ("v.object_id, v.l1_handle, v.binding_id, v.property, v.field_path, "
                "v.recorded_at, v.superseded_at, v.digest, v.lineage")
_VALUE_PARTS_PER_WORKER = 16   # object ranges per worker: an uneven range never idles them
_SNAPSHOT = re.compile(r"[0-9A-F]+-[0-9A-F]+(-[0-9]+)?")


def _workers(workers: int | None) -> int:
    """How many processes copy the values (None: every core; 0: none, the builder adds
    them row by row, as one process always did)."""
    return max(1, os.cpu_count() or 1) if workers is None else max(0, int(workers))


def _parts_in_order(job: Callable[..., str], jobs: Sequence[tuple[Any, ...]], workers: int,
                    folder: Path) -> Iterator[Path]:
    """``job(path, *args)`` for each ``args`` of ``jobs`` run by ``workers`` processes, at most
    two each ahead of the caller (each part waits on disk until it is taken); the parts'
    paths in the order of ``jobs``. A failed job raises here; the rest are cancelled."""
    import collections
    import concurrent.futures as cf
    import multiprocessing
    names = (str(folder / f"part-{i:06d}.sqlite") for i in range(len(jobs)))
    if workers <= 1:
        for name, args in zip(names, jobs):
            yield Path(job(name, *args))
        return
    import importlib
    from ontology.store import main_not_imported
    # named by this module's import name, never ``__main__`` (``python -m`` runs it as
    # that): a worker imports nothing of the build's main (main_not_imported)
    module = job.__module__ if job.__module__ != "__main__" else "ontology.step_migrate"
    job = getattr(importlib.import_module(module), job.__name__)
    with main_not_imported():
        pool = cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
    try:
        todo = iter(zip(names, jobs))
        pending: collections.deque[Any] = collections.deque()
        for name, args in todo:
            pending.append(pool.submit(job, name, *args))
            if len(pending) >= 2 * workers:
                break
        while pending:
            done = pending.popleft().result()
            for name, args in todo:
                pending.append(pool.submit(job, name, *args))
                break
            yield Path(done)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _input_filter(types: Sequence[str] | None) -> str:
    """``inp`` of :func:`copy_served` as a condition on ``o`` (an ``ontology.objects`` row)."""
    keep = "" if types is None else "o.object_type = ANY(%(types)s::text[]) AND "
    return (keep + "NOT EXISTS (SELECT 1 FROM unnest(%(dt)s::text[], %(dp)s::text[]) d(t, p) "
            "WHERE o.object_type = d.t AND starts_with(o.object_id, d.p))")


def _served_value_part(path: str, dsn: str, snapshot: str, types: Sequence[str] | None,
                       drop_types: Sequence[str], drop_prefixes: Sequence[str],
                       low: str | None, high: str | None) -> str:
    """A worker's part (``ontology.store.ValuePart``): the served values of the input
    objects in [``low``, ``high``) (None: unbounded), read under the copy's snapshot in
    the order the copy adds them."""
    import psycopg
    from ontology import store as S
    from ontology import store_ddl
    if not _SNAPSHOT.fullmatch(snapshot):
        raise ValueError("snapshot_malformed")
    sel = ", ".join(f"v.{c}" for c in store_ddl.VALUE_COLUMNS)
    where = [_input_filter(types)]
    params: dict[str, Any] = {"types": types, "dt": list(drop_types),
                              "dp": list(drop_prefixes)}
    if low is not None:
        where.append("v.object_id >= %(low)s")
        params["low"] = low
    if high is not None:
        where.append("v.object_id < %(high)s")
        params["high"] = high
    part = S.ValuePart(path)
    with psycopg.connect(dsn) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        pg.execute(f"SET TRANSACTION SNAPSHOT '{snapshot}'")
        # a range of objects: their versions, sources and cells by key, never a scan of
        # every cell joined by hash once per range
        pg.execute("SET LOCAL enable_hashjoin = off")
        pg.execute("SET LOCAL enable_mergejoin = off")
        with pg.cursor(name="copy_value_part") as named:
            named.execute(f"SELECT {sel} FROM ontology.object_values v JOIN ontology.objects o "
                          f"ON o.object_id = v.object_id WHERE {' AND '.join(where)} "
                          f"ORDER BY {_VALUE_ORDER}", params)
            while True:
                batch = named.fetchmany(20_000)
                if not batch:
                    break
                part.write(batch)
    return str(part.close())


def _layer_value_part(path: str, layer: str, low: str | None, high: str | None) -> str:
    """A worker's part: the unit layer's values of the objects in [``low``, ``high``)."""
    from ontology import store as S
    part = S.ValuePart(path)
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    try:
        for batch in S._value_rows(conn, low, high):
            part.write(batch)
    finally:
        conn.close()
    return str(part.close())


def _insert(builder: Any, table: str, rows: Iterable[Sequence[Any]]) -> None:
    """Rows of a plain table (``StoreBuilder._INSERT`` columns, in order) as
    ``builder.add`` inserts them, from tuples (no dict per row)."""
    cols = builder._INSERT[table]
    builder.connection.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                                   f"({', '.join('?' for _ in cols)})", rows)


def _fetched(cur: Any, sql: str, params: Any = (), size: int = 20_000
             ) -> Iterator[tuple[Any, ...]]:
    cur.execute(sql, params)
    while True:
        batch = cur.fetchmany(size)
        if not batch:
            return
        yield from batch


def _by_parts(builder: Any, workers: int) -> bool:
    """Whether the values go in by parts (a part's builder storing every value as a cell)."""
    return workers > 0 and getattr(builder, "_part", False) and not builder.packed_bindings


def _part_folder(builder: Any) -> tempfile.TemporaryDirectory:
    """Where the parts wait: beside the build's file (its disk), removed after."""
    return tempfile.TemporaryDirectory(prefix=".values-", dir=Path(builder.temp).parent)


def copy_served(dsn: str, builder: Any, *, types: Sequence[str] | None,
                unit: Mapping[str, Any] | None = None, workers: int | None = None,
                link_workers: int | None = None) -> Iterator[dict[str, Any]]:
    """Copy the served ontology's objects (of ``types``; None: every type) and their
    versions, removals, values and identifiers into ``builder``, leaving out what the unit
    migration ``unit`` (its record) deletes; yield every link touching them (the caller
    decides when each is shown to a step), in row order, the unit migration's links
    left out. Of the other objects' values, identifiers and links (none touching them)
    only the keys are copied (:data:`HELD`): the tripwire :func:`guard_inputs` lays over
    them. The values are read by ``workers`` processes (None: every core), each a range
    of the input objects under this copy's snapshot into a part the builder takes in
    order (``StoreBuilder.add_value_parts``: the same rows and ids as adding them one by
    one, which ``workers`` 0 does). ``link_workers``: the connections reading the links
    at once (None: up to 8)."""
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
            _insert(builder, "objects", _fetched(
                named, f"SELECT {sel} FROM ontology.objects o WHERE NOT EXISTS (SELECT 1 FROM "
                "unnest(%s::text[], %s::text[]) d(t, p) WHERE o.object_type = d.t AND "
                "starts_with(o.object_id, d.p)) ORDER BY o.row_no", (drop_types, drop_prefixes)))
        for table, order in (("object_versions", "ver"),
                             ("object_removals", "object_id, since"),
                             ("identifiers", "object_id, identifier_kind, normalized")):
            sel = ", ".join(f"t.{c}" for c in cols[table])
            with pg.cursor(name=f"copy_{table}") as named:
                _insert(builder, table, _fetched(
                    named, f"SELECT {sel} FROM ontology.{table} t JOIN inp USING (object_id) "
                    f"ORDER BY {order}"))
        n = _workers(workers)
        if _by_parts(builder, n):
            snapshot = cur.execute("SELECT pg_export_snapshot()").fetchone()[0]
            (count,) = cur.execute("SELECT count(*) FROM inp").fetchone()
            every = max(1, -(-count // (n * _VALUE_PARTS_PER_WORKER)))
            cuts = [r[0] for r in cur.execute(
                "SELECT object_id FROM (SELECT object_id, row_number() OVER (ORDER BY "
                "object_id) AS n FROM inp) x WHERE n > 1 AND (n - 1) %% %s = 0 "
                "ORDER BY object_id", (every,)).fetchall()]
            bounds = [None, *cuts, None]
            jobs = [(dsn, snapshot, types, list(drop_types), list(drop_prefixes), lo, hi)
                    for lo, hi in zip(bounds, bounds[1:])]
            with _part_folder(builder) as folder:
                builder.add_value_parts(_parts_in_order(_served_value_part, jobs, n,
                                                        Path(folder)))
        else:
            with pg.cursor(name="copy_values") as named:
                sel = ", ".join(f"v.{c}" for c in store_ddl.VALUE_COLUMNS)
                for batch in _rows(named, f"SELECT {sel} FROM ontology.object_values v JOIN "
                                          f"inp USING (object_id) ORDER BY {_VALUE_ORDER}"):
                    builder.add("object_values", batch)
        # the links: read raw from ``link_rows`` by every worker in row_no ranges under
        # this transaction's snapshot, their packed texts formatted here (the ``links``
        # view's link_text() per row was the copy's whole cost), shown in row order
        snapshot = cur.execute("SELECT pg_export_snapshot()").fetchone()[0]
        yield from _served_links(dsn, snapshot, types, drop_types, drop_prefixes,
                                 drop_links, workers=link_workers)


# The raw ``link_rows`` columns a served link is read from: the view's columns but the
# packed reason and evidence_handles, with what they are formatted from.
_RAW_LINK = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
             "link_contract", "reason", "reason_id", "why_not", "evidence_handles",
             "evidence_id", "ek", "slots", "reviewed_by", "reviewed_at", "review_action_id",
             "markings", "recorded_at", "superseded_at")
_LINK_CHUNK = 50_000          # rows a range holds, about


def _pg_format(skeleton: str, args: Sequence[str]) -> str:
    """Postgres ``format(skeleton, VARIADIC args)`` of a link skeleton (only %s and %%
    are written: :func:`ontology.store.pack_link_texts`); anything else, or too few
    texts, raises as format() would rather than reading differently."""
    out: list[str] = []
    rest = iter(args)
    i, last = 0, 0
    while True:
        i = skeleton.find("%", last)
        if i < 0:
            out.append(skeleton[last:])
            return "".join(out)
        out.append(skeleton[last:i])
        mark = skeleton[i + 1:i + 2]
        if mark == "%":
            out.append("%")
        elif mark == "s":
            text = next(rest, None)
            if text is None:
                raise ValueError("link_text_too_few_slots")
            out.append(text)
        else:
            raise ValueError(f"link_text_unknown_format:{skeleton[i:i + 2]!r}")
        last = i + 2


def _link_text(skeleton: str | None, slots: Sequence[int], texts: Mapping[int, str],
               split: dict[str, list[str] | None] | None = None) -> str | None:
    """``ontology.link_text()``: the skeleton (None: none) formatted with the slot texts
    (a slot without a text drops out, as the function's join drops it). ``split``: a
    cache of skeletons cut at their %s (None: one with %% or another mark, formatted by
    :func:`_pg_format`)."""
    if skeleton is None:
        return None
    args = [texts[t] for t in slots if t in texts]
    cut = (split or {}).get(skeleton, False)
    if cut is False:
        cut = skeleton.split("%s") if skeleton.count("%") == skeleton.count("%s") and \
            "%%" not in skeleton else None
        if split is not None:
            split[skeleton] = cut
    if cut is None:
        return _pg_format(skeleton, args)
    if len(args) < len(cut) - 1:
        raise ValueError("link_text_too_few_slots")
    out = [cut[0]]
    for text, piece in zip(args, cut[1:]):
        out += (text, piece)
    return "".join(out)


def _drop_table(drop_links: Sequence[Sequence[str]]
                ) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """The unit migration's link units by (link_type, binding_kind): the hashed side of
    the anti-join each served link is probed against."""
    table: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for t, k, f, o in drop_links:
        table.setdefault((t, k), []).append((f, o))
    return table


def _link_ranges(pg: Any, size: int | None = None) -> list[tuple[int, int] | None]:
    """Half-open row_no ranges covering every link row of about ``size`` rows each, in
    order; then None, the rows without a row_no (the ORDER BY's last)."""
    size = size or _LINK_CHUNK
    lo, hi = pg.execute("SELECT min(row_no), max(row_no) FROM ontology.link_rows").fetchone()
    out: list[tuple[int, int] | None] = []
    if lo is not None:
        est = pg.execute("SELECT reltuples FROM pg_class WHERE oid = "
                         "'ontology.link_rows'::regclass").fetchone()[0]
        est = int(est) if est and est > 0 else hi - lo + 1
        span = max(1, (hi - lo + 1) * size // max(est, 1))
        out = [(a, min(a + span, hi + 1)) for a in range(lo, hi + 1, span)]
    out.append(None)
    return out


def _link_sql(types: Sequence[str] | None) -> str:
    sel = ", ".join(f"l.{c}" for c in _RAW_LINK)
    where = "{range}"
    if types is not None:
        # the link touches an input object (``inp`` of copy_served: of ``types``, not one
        # the unit migration deletes); a key probe on objects per end
        obj = ("EXISTS (SELECT 1 FROM ontology.objects o WHERE o.object_id = l.{end} AND "
               "o.object_type = ANY(%(types)s::text[]) AND NOT EXISTS (SELECT 1 FROM "
               "unnest(%(dt)s::text[], %(dp)s::text[]) d(t, p) WHERE o.object_type = d.t "
               "AND starts_with(o.object_id, d.p)))")
        where += (f" AND ({obj.format(end='from_object')} OR "
                  f"{obj.format(end='to_object')})")
    return f"SELECT {sel} FROM ontology.link_rows l WHERE {where} ORDER BY l.row_no, l.link_id"


def _read_links(pg: Any, sql: str, rng: tuple[int, int] | None, params: dict[str, Any],
                drop: Mapping[tuple[str, str], Sequence[tuple[str, str]]]
                ) -> list[dict[str, Any]]:
    """One range's served links, as the ``links`` view reads them, the unit migration's
    left out."""
    from ontology import store_ddl
    if rng is None:
        rows = pg.execute(sql.replace("{range}", "l.row_no IS NULL"), params).fetchall()
    else:
        rows = pg.execute(sql.replace("{range}", "l.row_no >= %(lo)s AND l.row_no < %(hi)s"),
                          {**params, "lo": rng[0], "hi": rng[1]}).fetchall()
    ix = {c: i for i, c in enumerate(_RAW_LINK)}
    if drop:
        t, k, f, o = ix["link_type"], ix["binding_kind"], ix["from_object"], ix["to_object"]
        rows = [r for r in rows if not any(
            r[f].startswith(pf) and r[o].startswith(po) for pf, po in drop.get((r[t], r[k]), ()))]
    r_id, e_id, slots = ix["reason_id"], ix["evidence_id"], ix["slots"]
    skel_ids = {r[c] for r in rows for c in (r_id, e_id) if r[c] is not None}
    text_ids = {t for r in rows if r[r_id] is not None or r[e_id] is not None
                for t in r[slots]}
    skeletons = dict(pg.execute("SELECT id, text FROM ontology.link_skeletons WHERE "
                                "id = ANY(%s::bigint[])", (sorted(skel_ids),)).fetchall()
                     ) if skel_ids else {}
    texts = dict(pg.execute("SELECT id, text FROM ontology.link_texts WHERE "
                            "id = ANY(%s::bigint[])", (sorted(text_ids),)).fetchall()
                 ) if text_ids else {}
    reason, evidence, ek = ix["reason"], ix["evidence_handles"], ix["ek"]
    plain = [(c, ix[c]) for c in store_ddl.LINK_COLUMNS]
    out = []
    split: dict[str, list[str] | None] = {}
    for r in rows:
        row = {c: r[i] for c, i in plain}
        n = r[ek]
        if r[reason] is None:      # COALESCE(reason, link_text(reason_id, slots, ek + 1, 64))
            row["reason"] = _link_text(skeletons.get(r[r_id]), r[slots][n:64], texts, split)
        if r[evidence] is None:    # COALESCE(evidence_handles, link_text(.., slots, 1, ek))
            row["evidence_handles"] = _link_text(skeletons.get(r[e_id]), r[slots][:n], texts, split)
        out.append(row)
    return out


def _served_links(dsn: str, snapshot: str, types: Sequence[str] | None,
                  drop_types: Sequence[str], drop_prefixes: Sequence[str],
                  drop_links: Sequence[Sequence[str]], *, workers: int | None = None
                  ) -> Iterator[dict[str, Any]]:
    """Every served link (touching an object of ``types``; None: every link) but the unit
    migration's, in row order, as the ``links`` view reads them, under the exported
    ``snapshot``: ``workers`` connections read row_no ranges at once, shown in order."""
    import os
    import queue
    import threading
    import psycopg
    if not _SNAPSHOT.fullmatch(snapshot):
        raise ValueError("bad_snapshot")
    workers = max(1, workers or min(8, os.cpu_count() or 1))
    drop = _drop_table(drop_links)
    sql = _link_sql(types)
    params = {"types": list(types or ()), "dt": list(drop_types), "dp": list(drop_prefixes)}

    def connect() -> Any:
        pg = psycopg.connect(dsn)
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        pg.execute(f"SET TRANSACTION SNAPSHOT '{snapshot}'")
        return pg

    first = connect()
    try:
        ranges = _link_ranges(first)
    except BaseException:
        first.close()
        raise
    # ranges are taken in order and their results shown in order; at most ``ahead``
    # ranges read and not yet shown (memory stays bounded however slow the consumer)
    ahead = 2 * workers
    results: dict[int, Any] = {}
    cond = threading.Condition()
    state = {"next": 0, "shown": 0, "stop": False}

    def work(pg: Any) -> None:
        try:
            while True:
                with cond:
                    while (not state["stop"] and state["next"] < len(ranges)
                           and state["next"] - state["shown"] >= ahead):
                        cond.wait()
                    if state["stop"] or state["next"] >= len(ranges):
                        return
                    i = state["next"]
                    state["next"] += 1
                try:
                    got: Any = _read_links(pg, sql, ranges[i], params, drop)
                except BaseException as exc:   # shown to the reader at its turn
                    got = exc
                with cond:
                    results[i] = got
                    cond.notify_all()
                if isinstance(got, BaseException):
                    return
        finally:
            pg.close()

    threads = []
    try:
        conns = [first] + [connect() for _ in range(min(workers, len(ranges)) - 1)]
    except BaseException:
        first.close()
        raise
    for pg in conns:
        thread = threading.Thread(target=work, args=(pg,), daemon=True)
        thread.start()
        threads.append(thread)
    try:
        for i in range(len(ranges)):
            with cond:
                while i not in results:
                    if not any(t.is_alive() for t in threads):
                        raise RuntimeError("served_links_workers_died")
                    cond.wait(1.0)
                got = results.pop(i)
                state["shown"] = i + 1
                cond.notify_all()
            if isinstance(got, BaseException):
                raise got
            yield from got
    finally:
        with cond:
            state["stop"] = True
            cond.notify_all()
        for thread in threads:
            thread.join()


def copy_unit_layer(layer: str | Path, builder: Any, *, workers: int | None = None
                    ) -> Iterator[dict[str, Any]]:
    """Copy the unit migration layer's rows into ``builder``; yield its links. Its values
    are decoded by ``workers`` processes (None: every core; 0: none), as
    :func:`copy_served` reads the served ones."""
    from ontology import store as S
    from ontology import store_ddl
    conn = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = builder._INSERT
        for table in ("objects", "object_versions", "object_removals", "identifiers"):
            _insert(builder, table, (tuple(r) for r in conn.execute(
                f"SELECT {', '.join(cols[table])} FROM {table} ORDER BY rowid")))
        plain = sqlite3.connect(f"file:{layer}?mode=ro", uri=True)
        try:
            n = _workers(workers)
            if _by_parts(builder, n):
                (count,) = plain.execute("SELECT count(*) FROM objects").fetchone()
                bounds = S._object_bounds(plain, max(1, -(-count // (
                    n * _VALUE_PARTS_PER_WORKER))))
                jobs = [(str(layer), lo, hi) for lo, hi in zip(bounds, bounds[1:])]
                with _part_folder(builder) as folder:
                    builder.add_value_parts(_parts_in_order(_layer_value_part, jobs, n,
                                                            Path(folder)))
            else:
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


def _held_tables(conn: sqlite3.Connection, indexes: bool = True) -> None:
    for _view, table, cols, _key in HELD:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    if indexes:
        _held_indexes(conn)


def _held_indexes(conn: sqlite3.Connection) -> None:
    for table, indexes in _HELD_INDEXES.items():
        for cols in indexes:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_{'_'.join(cols)} ON "
                         f"{table}({', '.join(cols)})")


def _copy_held_keys(cur: Any, conn: sqlite3.Connection, drop_types: Sequence[str],
                    drop_prefixes: Sequence[str], drop_links: Sequence[Sequence[str]]) -> None:
    """Copy into ``conn`` the keys of the served values, identifiers and links of the
    objects outside the temporary ``inp`` (the declared types' objects), leaving out
    what the unit migration deletes."""
    _held_tables(conn, indexes=False)   # indexed once filled: far fewer page writes
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
    _held_indexes(conn)


def analyze_input(conn: sqlite3.Connection) -> None:
    """Measure the input's statistics (sampled: ``analysis_limit``) so a step's joins plan
    by them: unmeasured, document_fields' evidence join took an autoindex on property
    alone and ran quadratic. The store's fixed value_cells statistics
    (``store_ddl.PLANNER_STATS``) are kept as the store sets them."""
    from ontology import store_ddl
    conn.execute("PRAGMA analysis_limit = 400")
    conn.execute("ANALYZE")
    # the links measured whole: a sampled measure of an index over 25M links reads the
    # first few hundred entries of one object's or prefix's run and tells the planner
    # every object holds that many (the steps' per-lead reads then take another index)
    conn.execute("PRAGMA analysis_limit = 0")
    conn.execute("ANALYZE link_rows")
    conn.execute("PRAGMA analysis_limit = 400")
    fixed = [x for x in store_ddl.PLANNER_STATS]
    conn.executemany("DELETE FROM sqlite_stat1 WHERE tbl = ? AND idx = ?",
                     [(t, i) for t, i, _ in fixed])
    conn.executemany("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?, ?, ?)", fixed)
    conn.commit()
    conn.execute("ANALYZE sqlite_master")   # this connection plans with them too


class UndeclaredRead(RuntimeError):
    """A re-run step read an object type its ``Spec.inputs`` do not declare."""


TRIPWIRE_ENV = "PLX_STEP_TRIPWIRE"


def _tripwire(tripwire: bool | None) -> bool:
    """Whether the held rows' tripwire views are laid (None: ``PLX_STEP_TRIPWIRE`` is 1)."""
    return os.environ.get(TRIPWIRE_ENV) == "1" if tripwire is None else bool(tripwire)


def guard_inputs(conn: sqlite3.Connection, types: Sequence[str] | None,
                 step_prefixes: Iterable[str] = (), *, tripwire: bool | None = None) -> None:
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
    held. None: nothing is held.

    ``tripwire`` (None: ``PLX_STEP_TRIPWIRE=1``; the step migration tests run both ways)
    lays these tripwires. Without it (a build's default) every guarded view is its
    declared rows alone, held rows out of sight, and a read gives what it gives with
    the tripwire whenever the tripwire would not raise: a UNION ALL view keeps SQLite
    from pushing an IN-subquery or join constraint into it (a step's indexed read of
    ``links`` scanned every row once per probe), and the per-row tripwire of
    ``objects`` raised or not by the plan (a join reaching objects by key from an
    undeclared object's version), not by what the step reads."""
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
    # a read by type or by binding reaches only that type's rows (exactly answered, as
    # the held keys are): one of a declared type passes, one of another trips; without
    # these a filter on them scans every object and trips at the first undeclared one
    conn.execute("CREATE INDEX IF NOT EXISTS objects_held_by_type ON "
                 "objects_held(object_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS objects_held_by_binding ON "
                 "objects_held(binding_id)")
    conn.execute("ALTER TABLE identifiers RENAME TO identifiers_declared")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    trip = _tripwire(tripwire)
    conn.execute(f"CREATE VIEW objects AS SELECT * FROM objects_held WHERE object_type IN "
                 f"({allowed})" + (" OR json('plx-undeclared-read') IS NULL" if trip else ""))
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
        if not trip:
            conn.execute(f"CREATE VIEW {view} AS {select}")   # the declared rows alone
        else:
            trip = f"json('{{' || h.{key})"   # never JSON: raises when evaluated
            arm = ", ".join(f"h.{c}" if c in cols else trip for c in names)
            conn.execute(f"CREATE VIEW {view} AS {select} UNION ALL SELECT {arm} FROM "
                         f"{table} h WHERE (SELECT {trip}) IS NULL")   # checked last
        for trigger in triggers:
            conn.execute(trigger)
    conn.commit()


# The indexes the steps' reads of earlier steps' links search by (made once, before the
# first re-run step that ``reads`` another's links: the bulk writers before it never pay
# their upkeep) and measured whole, as the planner needs them measured: an identifier
# lead read by its object range (``mc_task_links.task_leads``) otherwise took the link_id
# range of every mention lead, and the distinct objects holding leads
# (``mention_firm._from_objects``) read every lead's table row. The plan guard
# (``tests/test_step_plan_guard``) explains every statement the steps issue under the
# served store's shape and fails on a full scan or a link id range taken over an
# object one.
READ_INDEXES = (
    "CREATE INDEX IF NOT EXISTS link_rows_by_kind_tier_from ON "
    "link_rows(binding_kind, tier, from_object)",
    "CREATE INDEX IF NOT EXISTS link_rows_by_id_tier_from ON "
    "link_rows(link_id, tier, from_object)",
)


def read_indexes(conn: sqlite3.Connection) -> list[str]:
    """Make :data:`READ_INDEXES` on the step input and measure them (every entry: a sampled
    measure of a lead index reads one link type's run); the names made."""
    made = []
    for ddl in READ_INDEXES:
        name = ddl.split(" ON ", 1)[0].rsplit(" ", 1)[1]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (name,)).fetchone() is None:
            conn.execute(ddl)
            made.append(name)
    if made:
        conn.execute("PRAGMA analysis_limit = 0")
        for name in made:
            conn.execute(f"ANALYZE {name}")
        conn.execute("PRAGMA analysis_limit = 400")
        conn.commit()
        conn.execute("ANALYZE sqlite_master")
    return made


def _layer_link_part(path: str, store: str, low: str, high: str,
                     froms: Sequence[str] | None) -> str:
    """A worker's part of the layer: the links of [``low``, ``high``) of the step input
    ``store`` (read-only) starting at ``froms`` (None: any), in link id order, added as
    the layer's builder adds them (each part interns its own texts; the merge maps them)."""
    from ontology import store_ddl
    from ontology.project_parallel import link_part
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    keep = tuple(froms) if froms else None

    def rows() -> Iterator[dict[str, Any]]:
        for r in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links WHERE "
                              "link_id >= ? AND link_id < ? ORDER BY link_id", (low, high)):
            if keep is None or r[2].startswith(keep):   # from_object
                yield dict(zip(store_ddl.LINK_COLUMNS, r))
    try:
        return link_part(path, rows())
    finally:
        conn.close()


_LINK_PARTS_PER_WORKER = 8
_LINK_PART_MIN_ROWS = 20_000   # fewer rows a part costs more than it saves


def _layer_link_jobs(conn: sqlite3.Connection, store: str, owned: Sequence[Mapping[str, Any]],
                     workers: int) -> list[tuple[Any, ...]]:
    """The layer's link parts: each owned prefix's link id range cut into pieces of about
    equal rows (one ordered pass of the link id index), in the order the rows go in."""
    jobs: list[tuple[Any, ...]] = []
    for o in owned:
        lo, hi = o["prefix"], o["end"]
        (count,) = conn.execute("SELECT count(*) FROM link_rows WHERE link_id >= ? AND "
                                "link_id < ?", (lo, hi)).fetchone()
        step = max(_LINK_PART_MIN_ROWS,
                   -(-count // max(1, workers * _LINK_PARTS_PER_WORKER)))
        cuts = [r[0] for r in conn.execute(
            "SELECT link_id FROM (SELECT link_id, row_number() OVER (ORDER BY link_id) AS n "
            "FROM link_rows WHERE link_id >= ? AND link_id < ?) WHERE n % ? = 1 AND n > 1",
            (lo, hi, step))] if count > step else []
        bounds = [lo, *cuts, hi]
        jobs += [(store, a, b, o["from"]) for a, b in zip(bounds, bounds[1:])]
    return jobs


def _ends_sql(column: str) -> str:
    """``f"{x.split(':', 1)[0]}:"`` of ``column`` in SQL."""
    return (f"CASE WHEN instr({column}, ':') > 0 THEN substr({column}, 1, instr({column}, "
            f"':')) ELSE {column} || ':' END")


def write_layer_links(layer: Any, conn: sqlite3.Connection, store: str | Path,
                      owned: Sequence[Mapping[str, Any]], workers: int,
                      folder: Path) -> tuple[int, set[str]]:
    """The re-derived links (every row of ``owned``'s prefixes from their from types) into
    the layer's builder ``layer`` from the step input ``store`` (``conn``: its committed
    connection): by ``workers`` processes writing parts merged in order by SQL (the rows,
    row ids and interned ids one process adding them in order gives: the merge interns
    each part's new texts in its order), or row by row (``workers`` 0). The count and
    the type prefixes of their ends."""
    from ontology import store_ddl
    if workers <= 0:
        n, ends, buf = 0, set(), []
        for o in owned:
            keep = tuple(o["from"]) if o["from"] is not None else None
            for r in conn.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM links "
                                  "WHERE link_id >= ? AND link_id < ? ORDER BY link_id",
                                  (o["prefix"], o["end"])):
                row = dict(zip(store_ddl.LINK_COLUMNS, r))
                if keep is None or row["from_object"].startswith(keep):
                    buf.append(row)
                    n += 1
                    ends.add(f"{row['from_object'].split(':', 1)[0]}:")
                    ends.add(f"{row['to_object'].split(':', 1)[0]}:")
                    if len(buf) >= 20_000:
                        layer.add("links", buf)
                        buf = []
        if buf:
            layer.add("links", buf)
        return n, ends
    from ontology.project_parallel import merge_link_parts
    main = layer.connection
    jobs = _layer_link_jobs(conn, str(store), owned, workers)
    try:
        merge_link_parts(layer, _parts_in_order(_layer_link_part, jobs, workers, folder))
    finally:
        main.execute("BEGIN IMMEDIATE")
    (n,) = main.execute("SELECT count(*) FROM link_rows").fetchone()
    ends = {r[0] for e in ("from_object", "to_object") for r in main.execute(
        f"SELECT DISTINCT {_ends_sql(e)} FROM link_rows")}
    return n, ends


def open_snapshots(sources: Mapping[str, Any]) -> dict[str, str]:
    """Open each document source's capture snapshot before the steps (``records([])``
    reads nothing): a source opened gives its groups a watermark, so the document steps'
    workers can read them (unopened, every group is read in the build's one process).
    The sources that failed, with the error's type (the step reads them here, slower)."""
    failed = {}
    for name, src in sources.items():
        if callable(getattr(src, "document_groups", None)) and \
                callable(getattr(src, "records", None)):
            try:
                src.records([])
            except Exception as exc:  # noqa: BLE001 - read in-process, as before
                failed[name] = type(exc).__name__
    return failed


# ------------------------------------------------------------------ steps side by side
#
# Steps that read none of each other's links run at once, each in a process forked from
# the build that reads the input (committed, unchanged meanwhile) and writes its links
# to a part of its own; the parts are merged in step name order, each with its step's
# turn (its from-type clear and its held links), as one process would have added them.
# The order is the steps' declared one: a step waits for every step whose links it
# ``reads`` (and ``reads_own``) and every step that ``amends`` its links; a step that
# reads captured documents, amends another's links or writes values runs in the build's
# own process. A step whose run reaches any other write (an update, an insert other than
# its links, a clear that finds rows, an attribute of the builder this does not offer)
# is run again in the build's process at its turn, as it always ran.

CONCURRENCY_ENV = "PLX_STEP_CONCURRENCY"


def _step_concurrency(given: int | None) -> int:
    if given is not None:
        return max(1, int(given))
    try:
        return max(1, int(os.environ.get(CONCURRENCY_ENV, "4")))
    except ValueError:
        return 1


def step_waits_for(name: str, specs: Mapping[str, Spec]) -> set[str]:
    """The steps ``name`` must follow (their links are what it reads)."""
    spec = specs[name]
    return (set(spec.reads) | set(spec.reads_own)
            | {other for other, s in specs.items() if name in s.amends}) - {name}


def side_by_side(spec: Spec) -> bool:
    """Whether a step may run in a process of its own (module comment)."""
    return spec.supported and not spec.documents and not spec.amends and not spec.values


def _wave(names: Sequence[str], i: int, run: Mapping[str, Any],
          specs: Mapping[str, Spec], width: int) -> tuple[list[str], int]:
    """The run steps from ``names[i]`` on that may run at once (at most ``width``), and
    the index after the last name their turns cover."""
    wave: list[str] = []
    j = i
    while j < len(names) and width > 1:
        name = names[j]
        if name in run:
            if not side_by_side(specs[name]) or step_waits_for(name, specs) & set(names[i:j]):
                break
            if len(wave) >= width:
                break
            wave.append(name)
        j += 1
    return wave, j


_LINK_STATEMENT = re.compile(r"\b(links|link_rows)\b", re.I)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def link_reads(sql: str, type_steps: Mapping[str, Iterable[str]],
               specs: Mapping[str, Spec]) -> set[str] | None:
    """The steps whose links statement ``sql`` (its bound values written in) reads: by
    the link types it names (``type_steps``: link type -> the steps writing links of it)
    and the link id prefixes; None when it reads links without naming a type or a
    prefix (any step's). A statement not on the links: none."""
    if not _LINK_STATEMENT.search(sql):
        return set()
    by_type: set[str] = set()
    by_prefix: set[str] = set()
    named = False
    for literal in (m.group(1).replace("''", "'") for m in _LITERAL.finditer(sql)):
        if literal in type_steps:
            by_type |= set(type_steps[literal])
            named = True
        step = step_of(literal, specs) if literal else None
        if step is not None:
            by_prefix.add(step)
            named = True
    if not named:
        return None
    # a link id range keeps the read to its steps' links whatever types it names
    return by_prefix or by_type


def undeclared_reads(name: str, statements: Iterable[str],
                     type_steps: Mapping[str, Iterable[str]],
                     specs: Mapping[str, Spec]) -> set[str]:
    """The steps (or "undetermined") whose links step ``name``'s ``statements`` read
    without its declaring them (:func:`step_waits_for`). Only steps before it in name
    order count: a later step's links are not there yet when it runs, one at a time or
    beside it."""
    allowed = step_waits_for(name, specs) | {name}
    out: set[str] = set()
    for sql in statements:
        if re.match(r"(?is)\s*(DELETE|INSERT|UPDATE)\b", sql):
            continue   # its own writes (a side-by-side step may only clear empty rows)
        owners = link_reads(sql, type_steps, specs)
        if owners is None:
            out.add("undetermined")
        else:
            out |= {o for o in owners - allowed if o < name}
    return out


class _NotHere(RuntimeError):
    """A side-by-side step reached a write only the build's own process may make."""


class _NoRows:
    rowcount = 0
    lastrowid = None
    description = None

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def fetchmany(self, *_: Any) -> list[Any]:
        return []

    def __iter__(self) -> Iterator[Any]:
        return iter(())


class _SideConnection:
    """The input as a side-by-side step reads it: read-only; a clear of rows that are
    not there (its own links, before it adds any) passes as the build's would; any
    other write raises."""

    def __init__(self, inner: sqlite3.Connection, builder: "_SideBuilder") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_builder", builder)

    def execute(self, sql: str, params: Any = ()) -> Any:
        head = sql.lstrip()
        if head[:6].upper() == "DELETE":
            if self._builder.added:
                raise _NotHere("delete_after_add")
            body = re.match(r"(?is)DELETE\s+(FROM\s.*)", head)
            if body is None or self._inner.execute("SELECT count(*) " + body.group(1),
                                                   params).fetchone()[0]:
                raise _NotHere("delete_finds_rows")
            return _NoRows()
        if re.match(r"(?i)(INSERT|UPDATE|REPLACE|BEGIN|COMMIT|END|SAVEPOINT|RELEASE|"
                    r"ROLLBACK|DROP|ALTER|CREATE\s+(UNIQUE\s+)?INDEX|ANALYZE|VACUUM)\b", head):
            raise _NotHere("write")
        return self._inner.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        raise _NotHere("write")

    def executescript(self, sql: str) -> Any:
        raise _NotHere("write")

    def commit(self) -> None:
        return None

    @property
    def in_transaction(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        if name in ("cursor", "create_function", "create_aggregate", "row_factory",
                    "text_factory", "total_changes", "close"):
            return getattr(self._inner, name)
        raise _NotHere(f"connection.{name}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in ("row_factory", "text_factory"):   # its trace stays
            raise _NotHere(f"connection.{name}=")
        setattr(self._inner, name, value)


class _SideBuilder:
    """The builder as a side-by-side step sees it: its links go to its part."""

    def __init__(self, store: str, part: Any) -> None:
        self._store = Path(store)
        self.added = False
        self.statements: list[str] = []
        self._part = part
        inner = sqlite3.connect(f"file:{store}?mode=ro", uri=True, isolation_level=None)
        inner.execute("PRAGMA cache_size = -262144")
        inner.set_trace_callback(self.statements.append)   # its reads, values written in
        self.connection = _SideConnection(inner, self)
        self._INSERT = part._INSERT
        self.packed_bindings = part.packed_bindings

    def add(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        if table != "links":
            raise _NotHere(f"add:{table}")
        self.added = True
        return self._part.add("links", rows)

    def __getattr__(self, name: str) -> Any:
        # ``temp`` too: worker processes the step starts would read the file untraced
        raise _NotHere(f"builder.{name}")


def _side_step(name: str, step: Callable[..., Any], new: Any, sources: Mapping[str, Any],
               normalizer: Any, store: str, part_path: str, send: Any,
               type_steps: Mapping[str, Iterable[str]], specs: Mapping[str, Spec]) -> None:
    """A forked process: ``step`` over the input, its links into a part; sends
    ("ok", report, part, seconds) or ("here", why)."""
    from ontology.store import StoreBuilder
    t = time.monotonic()
    part = StoreBuilder(Path(part_path), foreign_keys=False)
    try:
        builder = _SideBuilder(store, part)
        result = step(builder, _projector(new, sources, normalizer))
        wrong = undeclared_reads(name, builder.statements, type_steps, specs)
        if wrong:   # it read links of a step it may run beside: its turn, one at a time
            raise _NotHere("undeclared_read:" + ",".join(sorted(wrong)))
        path = part.keep_as_part()
        send.send(("ok", result, str(path), time.monotonic() - t))
    except BaseException as exc:  # noqa: BLE001 - run again in the build's process
        part.abort()
        why = str(exc) if isinstance(exc, _NotHere) else type(exc).__name__
        try:
            send.send(("here", why))
        except Exception:  # noqa: BLE001 - an unpicklable report: run it there
            pass
    finally:
        send.close()


STEP_TIMEOUT_ENV = "PLX_STEP_SIDE_TIMEOUT"   # seconds a side-by-side step may take


def run_side_by_side(wave: Sequence[str], steps: Mapping[str, Any], new: Any,
                     sources: Mapping[str, Any], normalizer: Any, store: str, folder: Path,
                     run: Mapping[str, Any], specs: Mapping[str, Spec],
                     type_steps: Mapping[str, Iterable[str]] | None = None,
                     timeout: float | None = None) -> dict[str, tuple[Any, ...]]:
    """Run the ``wave`` steps at once, each in a forked process (module comment): step ->
    ("ok", report, part, seconds) or ("here", why). Never forks a process running other
    threads (a forked child could wait forever on a lock one of them held): then every
    step is ("here", "threads"). A step past ``timeout`` seconds (None:
    ``PLX_STEP_SIDE_TIMEOUT``, else 4 h) is ended (its process only) and runs here."""
    import multiprocessing
    import threading
    if threading.active_count() > 1:
        return {name: ("here", "threads") for name in wave}
    if timeout is None:
        timeout = float(os.environ.get(STEP_TIMEOUT_ENV, 4 * 3600))
    ctx = multiprocessing.get_context("fork")
    procs = {}
    for name in wave:
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_side_step, args=(
            name, steps[name], new, sources, normalizer, store,
            str(folder / f"step-{name}.sqlite"), send, dict(type_steps or {}), specs),
            daemon=True)
        proc.start()
        send.close()
        procs[name] = (proc, recv)
    out: dict[str, tuple[Any, ...]] = {}
    deadline = time.monotonic() + timeout
    for name, (proc, recv) in procs.items():
        try:
            if recv.poll(max(0.0, deadline - time.monotonic())):
                out[name] = recv.recv()
            else:
                out[name] = ("here", "timeout")
                proc.kill()   # this step's own process only
        except EOFError:
            out[name] = ("here", "process_ended")
        proc.join()
        if proc.exitcode not in (0, None) and out[name][0] == "ok":
            out[name] = ("here", f"exit_{proc.exitcode}")
    return out


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


def text_marks(projector: Any) -> tuple[set[str], dict[str, Any]]:
    """The version handles (document citations) whose text fields this code's text index
    marks ``finance`` and ``pii`` (``ontology.text_index._sensitive``), judged on every
    capture source's documents as the index reads them (``source_document_groups``,
    through the build's kept readings: no file is read again), and the counts. Fail
    closed: a group that cannot be read raises (its documents' fields cannot be known
    unmarked), as does a document the judge cannot read."""
    from ontology.mentions import source_document_groups
    from ontology.text_index import _sensitive
    marked: set[str] = set()
    counts = {"documents": 0, "documents_marked": 0, "groups": 0}
    failed = 0
    for _group, load in source_document_groups(projector):
        counts["groups"] += 1
        try:
            documents, _remembered = load()
        except (sqlite3.Error, MemoryError, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001 - counted, then refused below
            failed += 1
            continue
        for document in documents:
            counts["documents"] += 1
            if _sensitive(document):
                marked.add(document.citation)
    if failed:
        raise ValueError(f"text_marks_groups_unread:{failed}")
    counts["documents_marked"] = len(marked)
    return marked, counts


def _text_marks_scope(dsn: str, handles: Sequence[str]) -> list[str]:
    """The object id prefixes (``<Type>:``) of the served objects holding a text field of
    one of ``handles`` (the step migration's scope: a delta layer replacing one since the
    snapshot is queued to be projected again)."""
    if not handles:
        return []
    import psycopg
    with psycopg.connect(dsn) as pg:
        rows = pg.execute("SELECT DISTINCT split_part(object_id, ':', 1) FROM "
                          "ontology.text_fields WHERE version_handle = ANY(%s)",
                          (sorted(handles),)).fetchall()
    return sorted(f"{r[0]}:" for r in rows)


def build_layer(the_plan: Mapping[str, Any], new: Any, steps: Mapping[str, Any], dsn: str,
                out: str | Path, *, sources: Mapping[str, Any] | None = None,
                unit_layer: str | Path | None = None, normalizer: Any = None,
                delta_mark: int = 0, specs: Mapping[str, Spec] = STEPS,
                sequence: int | None = None, workers: int | None = None,
                steps_side_by_side: int | None = None) -> dict[str, Any]:
    """Run ``the_plan``'s steps (``steps``: name -> the build's step function, this tree's
    ``operating.build_steps.post_project``) against the served ontology at ``dsn`` (with
    ``unit_layer`` laid over it) and write their links into the store ``out`` (replaced)
    with the migration record. ``sources``: the capture sources a document step reads
    (with the build's kept readings entered by the caller). ``workers``: the processes
    reading the input's values (None: every core; 0: the builder adds them row by row).
    ``steps_side_by_side``: how many independent steps run at once (None:
    ``PLX_STEP_CONCURRENCY``, else 4; 1: one after another; :func:`run_side_by_side`).
    Returns the report."""
    from ontology.project import open_generation
    from ontology.schema_migrate import read_layer
    from ontology.store import StoreBuilder
    from ontology import store_ddl

    run = dict(the_plan["steps"]["run"])
    remarks = set(the_plan["steps"].get("remark") or ())   # re-marked in place
    marked: set[str] = set()
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
    values_owned = _owned_values(run, specs)
    values_where, values_params = owned_values_where(values_owned)
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
        # link type -> the steps whose served links are of it (what a step reading the
        # type reads), and the types the steps declare they write
        type_steps: dict[str, set[str]] = {}
        for step_name, spec in specs.items():
            for t in (spec.writes(new) if spec.writes is not None else ()):
                type_steps.setdefault(t, set()).add(step_name)

        def take(rows: Iterable[dict[str, Any]]) -> None:
            nonlocal shown, dropped
            for row in rows:
                if _is_owned(row["link_id"], row["from_object"], owned):
                    dropped += 1           # the step derives it again
                    owner = step_of(row["link_id"], specs)
                    if owner is not None:
                        type_steps.setdefault(row["link_type"], set()).add(owner)
                    continue
                name = step_of(row["link_id"], specs)
                if name is not None:
                    type_steps.setdefault(row["link_type"], set()).add(name)
                if name is not None and first is not None and name >= first:
                    held.setdefault(name, []).append(row)   # shown once its step's turn came
                    continue
                pending.append(row)
                shown += 1
                if len(pending) >= 20_000:
                    builder.add("links", pending)
                    pending.clear()

        take(copy_served(dsn, builder, types=inputs, unit=unit, workers=workers))
        if unit_layer is not None:
            take(copy_unit_layer(unit_layer, builder, workers=workers))
        if pending:
            builder.add("links", pending)
            pending.clear()
        # the served values of the re-run steps go: each derives them again (a step
        # writing values runs first in name order, so no step before it saw them)
        values_dropped = 0
        if values_owned:
            values_dropped = conn.execute("SELECT count(*) FROM object_values WHERE "
                                          + values_where, values_params).fetchone()[0]
            conn.execute("DELETE FROM object_values WHERE " + values_where, values_params)
        report["input"] = {"links_shown": shown, "links_rederived": dropped,
                           "values_rederived": values_dropped,
                           "links_held": sum(len(v) for v in held.values()),
                           "types": inputs,
                           "seconds": round(time.monotonic() - started, 3)}
        conn.commit()
        guard_inputs(conn, inputs,   # the unit layer's other types held too
                     [p for spec in specs.values() for p in spec.prefixes])
        analyze_input(conn)
        indexed = False

        def index_for(names: Iterable[str]) -> None:
            nonlocal indexed
            if not indexed and any(specs[n].reads for n in names):
                indexed = True
                t = time.monotonic()
                report["read_indexes"] = {"made": read_indexes(conn),
                                          "seconds": round(time.monotonic() - t, 3)}

        def run_here(name: str, mode: str = "here") -> None:
            t = time.monotonic()
            spec = specs[name]
            index_for([name])
            srcs = _sources_of(new, sources, run[name]) if spec.documents else sources
            projector = _projector(new, srcs, normalizer)
            if name in remarks:   # what it marks, never its rows written again
                def call(_builder: Any, projector: Any) -> dict[str, Any]:
                    found, counts = text_marks(projector)
                    marked.update(found)
                    return counts
            else:
                call = steps[name]
            try:
                result = call(builder, projector)
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
                                     "report": result, "mode": mode}

        def finish(name: str) -> None:
            if name in run and run[name] is not None:
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

        names = [n for n in order if first is not None and n >= first]
        side = _step_concurrency(steps_side_by_side)
        i = 0
        while i < len(names):
            wave, j = _wave(names, i, run, specs, side)
            if len(wave) < 2 or remarks & set(wave):
                if names[i] in run:
                    run_here(names[i])
                finish(names[i])
                i += 1
                continue
            index_for(wave)
            conn.commit()
            with _part_folder(builder) as folder:
                done = run_side_by_side(wave, steps, new, sources, normalizer,
                                        str(builder.temp), Path(folder), run, specs,
                                        type_steps)
                for name in names[i:j]:
                    if name in done and done[name][0] == "ok":
                        _, result, part, seconds = done[name]
                        t = time.monotonic()
                        from ontology.project_parallel import merge_link_parts
                        merge_link_parts(builder, [Path(part)], rebuild_indexes=False)
                        report["steps"][name] = {
                            "seconds": round(seconds + time.monotonic() - t, 3),
                            "report": result, "mode": "side_by_side"}
                    elif name in run:
                        run_here(name, "here:" + (done[name][1] if name in done else "-"))
                    finish(name)
            i = j
        # the re-derived values, with their objects' own and version rows (the layer's
        # value rows name their version; the apply copies only the values)
        value_rows: list[dict[str, Any]] = []
        value_objects: list[dict[str, Any]] = []
        value_versions: list[dict[str, Any]] = []
        if values_owned:
            value_rows = [dict(zip(store_ddl.VALUE_COLUMNS, r)) for r in conn.execute(
                f"SELECT {', '.join(store_ddl.VALUE_COLUMNS)} FROM object_values WHERE "
                + values_where + " ORDER BY object_id, l1_handle, binding_id, property",
                values_params)]
            ids = json.dumps(sorted({r["object_id"] for r in value_rows}))
            for table, sink in (("objects", value_objects),
                                ("object_versions", value_versions)):
                cols = builder._INSERT[table]
                sink += [dict(zip(cols, r)) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table} WHERE object_id IN "
                    "(SELECT value FROM json_each(?)) ORDER BY rowid", (ids,))]
        conn.commit()   # the layer's workers read the input as it stands
        t = time.monotonic()
        layer = StoreBuilder(out, foreign_keys=False)
        try:
            open_generation(layer)
            # the re-derived links: every row of the steps' prefixes from their from
            # types, in link id order, never all held at once
            with _part_folder(builder) as folder:
                links, ends = write_layer_links(layer, conn, builder.temp, owned,
                                                _workers(workers), Path(folder))
            for table, rows in (("objects", value_objects),
                                ("object_versions", value_versions),
                                ("object_values", value_rows)):
                for i in range(0, len(rows), 20_000):
                    layer.add(table, rows[i:i + 20_000])
            layer.set_meta("format", FORMAT)
            layer.set_meta("steps_digest", the_plan["steps_to"])
            c = layer.connection
            if remarks:
                c.execute(f"CREATE TABLE {TEXT_MARKS} (version_handle TEXT PRIMARY KEY)")
                c.executemany(f"INSERT INTO {TEXT_MARKS} VALUES (?)",
                              [(h,) for h in sorted(marked)])
            scope = sorted(ends | {p for o in owned if o["from"] for p in o["from"]}
                           | {o["prefix"] for o in values_owned}
                           | set(_text_marks_scope(dsn, sorted(marked)) if remarks else ()))
            from ontology.schema_migrate import content_digest
            content = content_digest(c, ("link_rows", "objects", "object_versions",
                                         "value_sources", "value_cells")
                                     + ((TEXT_MARKS,) if remarks else ()))
            sequence = int(time.time() * 1000) if sequence is None else int(sequence)
            unit_id = unit["id"] if unit is not None else None
            record = {"id": f"{_sha([the_plan['id'], unit_id, content, sequence])[:16]}"
                            f"-{sequence}",
                      "plan": the_plan["id"], "unit": unit_id, "content": content,
                      "sequence": sequence, "from": the_plan["steps_from"],
                      "to": the_plan["steps_to"], "owned": owned, "values": values_owned,
                      "scope": scope,
                      "steps": sorted(run), "inputs": inputs, "delta_mark": int(delta_mark),
                      "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if remarks:   # the apply adds ``marks`` to the text fields of these documents
                record["text_marks"] = {"steps": sorted(remarks), "marks": list(SENSITIVE_MARKS),
                                        "documents": len(marked)}
            report["id"] = record["id"]
            c.execute(f"CREATE TABLE {TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.executemany(f"INSERT INTO {TABLE} VALUES (?, ?)",
                          [(k, json.dumps(v, sort_keys=True)) for k, v in record.items()])
            path = layer.keep_as_part()
        except BaseException:
            layer.abort()
            raise
        report["layer_seconds"] = round(time.monotonic() - t, 3)
    finally:
        builder.abort()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{work}{suffix}").unlink(missing_ok=True)
    path.replace(out)
    if remarks:
        report["text_marks"] = len(marked)
    report.update(links=links, values=len(value_rows), scope=scope,
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
        unopened = open_snapshots(sources)
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(projection / READINGS))
        stack.enter_context(kept_members(projection / MEMBERS, blobs))
        tmp = scratch or Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix=".step-migrate-", dir=out.parent)))
        steps = build_steps.post_project(state, tenant, scratch=tmp)
        from ontology.schema_migrate import next_sequence
        report = build_layer(the_plan, new, steps, dsn, out, sources=sources,
                             unit_layer=unit_layer, delta_mark=mark,
                             sequence=next_sequence(dsn))
        report["snapshots_unopened"] = unopened
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.step_migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="the unit and step migration from a commit's code")
    p.add_argument("--from-ref", required=True)
    p.add_argument("--served-code", default=None,
                   help="the served step code pin (code + steps_digest) when no commit holds it")
    b = sub.add_parser("build", help="the step migration layer over the served ontology")
    b.add_argument("--from-ref", required=True)
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--unit-layer", default=None,
                   help="the unit migration layer applied in the same transaction")
    b.add_argument("--dsn", default=None)
    b.add_argument("--served-code", default=None,
                   help="the served step code pin (code + steps_digest) when no commit holds it")
    args = parser.parse_args(argv)
    the_plan = plan(args.from_ref, old_code=served_code(args.served_code)
                    if args.served_code else None)
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
