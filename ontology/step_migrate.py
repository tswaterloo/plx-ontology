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
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from ontology import parent_watch

from ontology import step_code
from ontology.step_code import _read, _sha

FORMAT = "plx-step-migration/v1"
TABLE = "step_migration"
ROOT = Path(__file__).resolve().parents[1]
MENTION = "mention"


def _get(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)


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


# each step's code files: a table of data (``ontology/step_code.json``), so a build records
# the steps digest (``step_code``) without importing this module
CODE: dict[str, tuple[str, ...]] = step_code.step_files() or {}
# and the definitions the build runs for each (the v2 table: a step's digest is the code its
# roots reach in its files, not their bytes; None: a v1 table, the byte scheme)
ROOTS: dict[str, tuple[tuple[str, str | None], ...]] | None = step_code.step_roots()

STEPS: dict[str, Spec] = {
    # kind_confirmation.PROJECTED_TYPE / PROJECTED_BINDING (a test holds them equal)
    "confirmations": Spec((), CODE["confirmations"],
                          values=(("SharePointFile", "sharepoint.drive_items"),),
                          inputs=_confirmation_inputs),
    # the readings it parses (captured_text, document_understanding) are the units'
    # derivation code (``project._DERIVATION_ROOTS``, the unit digest): this step's roots
    # reach only ``document_understanding.EXTRACTOR_VERSION``
    "document_fields": Spec(("docfield:",), CODE["document_fields"],
                            writes=_writes_document_fields, inputs=_mention_inputs(True),
                            from_inputs=_mention_froms(True)),
    # the identifier grammar is the extractor's (``mentions.extractor_version``): a pattern
    # change re-derives the step's links (live 2026-11-12: a narrower petra_po pattern
    # left 58,086 stale links, the step's own code unchanged)
    "mentions": Spec(("mention:",), CODE["mentions"],
                     writes=_writes_mentions, documents=True, inputs=_mention_inputs(False),
                     from_inputs=_mention_froms(False), properties=_mention_properties),
    "key_joins": Spec(("keyjoin:",), CODE["key_joins"],
                      writes=_key_join_links, inputs=_key_join_inputs),
    "reviews": Spec(("rvw-",), CODE["reviews"], writes=_reviewed_links,
                    amends=("document_fields", "mentions"), inputs=_reviews_inputs),
    "roles": Spec(("roles:",), CODE["roles"],
                  writes=_named_links("Customer.same_party_as_supplier"),
                  inputs=_fixed("Customer", "Supplier")),
    "sender_corroboration": Spec(("docfirm:", "doccarrier:"),
                                 CODE["sender_corroboration"],
                                 reads=("document_fields",), documents=True,
                                 inputs=_union(_stepped_link_ends, _identifier_types,
                                               _twin_inputs),
                                 properties=_sender_properties),
    "sender_parties": Spec(("sendparty:",), CODE["sender_parties"],
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
    # formula sheets' lines: the sheets' names and text, the products' codes and names,
    # the parts' identifiers and the item twins (ontology.formula_lines)
    "with_formula_lines": Spec(("formula:",), CODE["with_formula_lines"],
                               writes=_named_links("Product.formula_line",
                                                   "FgItem.formula_line"),
                               reads=("twins_parties",),
                               inputs=_fixed("Attachment", "ContainerMember", "SharePointFile",
                                             "Product", "FgItem")),
    "with_owners": Spec(("owner:",), CODE["with_owners"],
                        writes=_named_links("AssemblyOrder.created_by", "PurchaseOrder.buyer",
                                            "SalesOrder.sales_rep",
                                            "Customer.account_owner", "PlannerTask.assigned_to",
                                            "MCTask.assigned_to", "MCTask.reported_by",
                                            "MCApproval.requested_by_person",
                                            "MCApproval.decided_by_person"),
                        # its contradicted MCTask.assigned_to links: a type the task id
                        # and name steps write too (every MCTask link type)
                        reads=("task_ids", "task_names"),
                        inputs=_fixed("Person", "AssemblyOrder", "PurchaseOrder",
                                      "SalesOrder", "Customer", "PlannerTask", "MCTask",
                                      "MCApproval")),
    "with_people": Spec(("person:",), CODE["with_people"],
                        writes=_named_links("Email.sent_by_person", "Email.addressed_to_person",
                                            "CalendarEvent.with_person", "Meeting.with_person",
                                            "TeamsChatMessage.sent_by_person",
                                            "TeamsChannelMessage.sent_by_person",
                                            "TeamsChat.with_person",
                                            "TeamsChannel.with_person",
                                            "PlannerTask.with_person", "MCTask.with_person",
                                            "MCApproval.with_person", "Contact.same_person_as",
                                            "Person.same_person_as"),
                        inputs=_fixed("Person", "Email", "CalendarEvent", "Meeting",
                                      "TeamsChatMessage", "TeamsChannelMessage", "TeamsChat",
                                      "TeamsChannel", "PlannerTask", "MCTask", "MCApproval",
                                      "Contact")),
    # demand pegging reads open order and PO lines, their orders, items and assembly orders
    "supply_pegging": Spec(("peg:",), CODE["supply_pegging"],
                           writes=_named_links("SalesOrderLine.supplied_by",
                                               "SalesOrder.awaits_purchase_order"),
                           inputs=_fixed("SalesOrder", "SalesOrderLine", "PurchaseOrder",
                                         "PurchaseOrderLine", "Product", "AssemblyOrder",
                                         "AssemblyOrderAllocation", "AssemblyOrderComponent")),
    "task_ids": Spec(("crb-mcid:",), CODE["task_ids"],
                     writes=_from_type_links("MCTask"), reads=("mentions",),
                     inputs=_union(_fixed("MCTask"), _identifier_types)),
    "task_names": Spec(("mcname:",), CODE["task_names"],
                       writes=_from_type_links("MCTask"),
                       inputs=_fixed("MCTask", "Customer", "Supplier", "Product")),
    # the text index's inputs are the apply's: a unit migration re-marks the text fields
    # of the objects it rewrites and refuses what it cannot (ontology_migrate._text_index);
    # a change of its code is refused unless it only marks (MARKS_ONLY: re-marked in place)
    "text_index": Spec((), CODE["text_index"],
                       # it writes no links, so no person's rejection of a link reaches it
                       # (a reviews code change is no reason to re-index text)
                       reads_own=(), supported=False, inputs=_fixed()),
    "trace_postings": Spec(("postdoc:",), CODE["trace_postings"],
                           writes=_posting_links, inputs=_posting_inputs),
    "twins": Spec(("crb-po-",), CODE["twins"], inputs=_twin_inputs),
    # two cards of one BC company with one name and one e-mail or phone (contact_twins)
    "twins_contacts": Spec(("crb-contact-",), CODE["twins_contacts"],
                           writes=_named_links("Contact.same_record_as"),
                           inputs=_fixed("Contact")),
    "twins_parties": Spec(("crb-party-",), CODE["twins_parties"],
                          reads=("twins",), inputs=_party_twin_inputs),
    "unique_mentions": Spec(("crb-mid:",), CODE["unique_mentions"],
                            # po_mention_resolve reads the order and party twins
                            writes=_firm_mention_links,
                            reads=("mentions", "twins", "twins_parties"),
                            # a reused order number's resolution reads the orders, their
                            # suppliers and the messages' senders (po_mention_resolve)
                            inputs=_union(_mention_inputs(False), _identifier_types,
                                          _fixed("PurchaseOrder", "Supplier", "Contact",
                                                 "Customer", "Email")),
                            from_inputs=_mention_froms(False)),
    "your_reference": Spec(("yrf-po-",), CODE["your_reference"],
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
         "134dfec62ca16d17b649eed64dabcd8b1ad3db3c740c9fd1037831b5f99e801b"),
        # 8a3ac9ab84 (train 2): text_parallel's pool ends with its parent; 134dfec6 ->
        # badc3269 writes the same rows (SAME_ROWS below), so it is the same marks target
        ("becab53876388be8bf17db03631e2af2ecbd9c89eeb814e3b715dc6c0d6c403f",
         "badc326924d02c09f2db6c7fdfd44d20642de7e4bea168bcaf2aaa4f2a01d8e5")}),
}
# Code changes of a step proven to write the same rows, as (from, to) pairs of its code
# digest (:func:`code_digests`), each pinned by reading the change: a step migration across
# one does not run the step again for its code (its link types and inputs still decide),
# and the served step code still switches to the new digest. Any other pair is a change.
# c7035f86f1 (main, 11-09): the SQLite DDL of a module's own side store moved verbatim into
# ``operating/declared_schema`` (version 1 is the earlier DDL, idempotent): document_fields'
# kept-fields memo (``KeptFields``) and kind_confirmation's confirmation store. Neither
# step's rows read or depend on that DDL. From the build of 11-10 (fb77cd38eb's step code) to the tree that moved it.
# with_owners, 352991482b..ed679e9772 (main, 11-11): distribution-list membership in
# ``integrations/people_exclusion.py``. Every change is on the bound guard (a live tenant
# listing: ``list_groups``, ``_group_excluded``) or the recorded listing (``attach_listing``,
# ``tenant_listing_rows``, ``mail_capture_listing``); the step's guard is
# ``PeopleExclusion.from_env`` (never bound, no listing or directory attached), whose
# ``from_env``, ``decides`` and ``is_excluded`` on object ids are unchanged. From the served
# step code (be37cefd36, the in-place run of 11-11) to ed679e9772. Old and new code agreed
# on all 92,248 served Person ids and addresses and on the step's fixture links.
# with_owners, 0bfbe020a8..defb027118 (main, 11-12): ``PeopleExclusion.fingerprint`` added
# to ``integrations/people_exclusion.py`` (a digest of the guard's ids for the commitments
# cursor); nothing the step runs calls it, and no other line of the step's files moved.
# From the served step code (eab7a9759c's pin target) to defb027118. Old and new code
# agreed on all 92,248 served Person object ids and addresses (3 exclusion settings) and
# on the step's fixture links.
# twins and your_reference, 4029b28005..2de675a044 (C-07 with identity-1 C-06, 11-12):
# ``identity_scoring.py`` is their one code file, and the change is ``_PartyTwin`` (the
# ``two_companies`` outcome of ``judge``; ``partners`` reads stored-key links only) and the
# ``_PARTY_OUTCOMES`` tally of the ``twins_parties`` step. Neither name is
# reachable from ``twin_step`` or ``your_reference_step`` (every name and attribute their
# code reads, classes whole); the identifier grammar change beside it (C-03) is the
# mention profiles, which neither step reads (they parse values by ``kinds``).
# with_people, 062ecdc47b..(the combined migration's part 1, 11-12): its code files moved
# only by DOC-002's ``document_fields._parse`` compound-unit refusal (``_COMPOUND_UNIT``)
# and an ``_excluder`` people_links defines for owner_links and never calls. Resolved by
# module, the step reaches document_fields only through ``profiles`` (sender_parties
# ``own_domains``), which reaches neither. Its alias change (identity-1) is part 2's.
# with_people, CT-D-06 (11-12): ``CertificateOfAnalysis.for_lot`` is derived inside
# ``document_fields.store_document_links`` (``_holder_candidates``, ``_lot_links`` and their
# helpers; the document_fields step runs again for it and DOC-002 in the one part 1 run).
# with_people's one module crossing, ``profiles``, reaches none of these names. The same
# served step code (062ecdc47b) to the part 1 tree with it (f83d28080d).
# with_people, DOC-001/003/004/006 (11-12, trust-2): ``date_order``, ``carrier_promises``,
# the fields memo (``_fields_of``, ``_bytes_kept``, ``_documents``) in document_fields and
# the reading modules it imports (captured_text's memo scope and ``reading_kept``,
# document_understanding's hidden HTML); ``profiles`` reaches none of them. The same served
# step code to the part 1 tree with them (5bb94a934a), replacing f83d28080d's pair.
# text_index and sender_corroboration, a725d74f07 (train 4b, 11-14) -> train2-1114: their
# one change is the worker pools' initializer (``**parent_watch.pool_init(_init, args)``,
# perm-box-1114-steps 8a3ac9ab84): the workers run ``_init(*args)`` as before and also end
# with their parent; nothing they read or write moved (text_parallel.py, sender_parallel.py).
SAME_ROWS: dict[str, frozenset[tuple[str, str]]] = {
    "text_index": frozenset({
        ("134dfec62ca16d17b649eed64dabcd8b1ad3db3c740c9fd1037831b5f99e801b",
         "badc326924d02c09f2db6c7fdfd44d20642de7e4bea168bcaf2aaa4f2a01d8e5")}),
    "sender_corroboration": frozenset({
        ("2da89ef877c4f4a44802c76b8af2cc7ba697c1fac85a72059c650e45966b6058",
         "a5a65898f83ffcc0ee77f731a1ca3307bae87657488921d9f56812993b9f1200")}),
    "with_people": frozenset({
        ("4d746596a06bc0c717a9d387183a8a38c5b2308bcf1df835cbb16de11a8ee6ce",
         "36d660ba7aa04ecb6b9a2befcf5fe40ee06b6f9dd5f5177c74e031f7f5e344e5"),
        ("4d746596a06bc0c717a9d387183a8a38c5b2308bcf1df835cbb16de11a8ee6ce",
         "9742bcf6d6538a7ffccc733ab423e69f69bd1dcb9e8e9bbd2e727dee830f0d1a"),
        # NW-13 (7edac7f84d): a batch of objects per read, equal row for row
        # (ontology/tests/test_step_reads.py at 1/10/60 objects)
        ("4d746596a06bc0c717a9d387183a8a38c5b2308bcf1df835cbb16de11a8ee6ce",
         "aeed06f808cf7be5f4f195ae65ff32716c2ad01dac9dc1f911da678db2218dc0"),
        # blob-zstd on c387ba7453 (train 3's PIN): document_fields' _bytes_held tests a held
        # form through blob_held; the step (people_links_step) reaches document_fields only
        # through ``profiles``, which reaches no name the change touched
        ("21fd8ef42a0954e3f8b605ebbcc85de9b4a9f4b39b252e453d6d760ec54ae57d",
         "f38da8fa9aa4706248c1575265c2ce0ea6bb0af923150dba232b4637dc52bfd6"),
        # TRAIN2-B1 (v2 scheme; compare_code of c387ba7453, f9bdbbbcb3 and M are all the
        # first): alias_pairs takes a part (k, n) and people_links_step passes conn.part.
        # Unparted, part is None (SplitConnection.part's default; a SQLite build's
        # connection has none), mine() is True for every Person, and the three added
        # ``not mine`` tests never skip: the same pairs, rows, order and counts. Only a
        # parted run (pg_step_inplace.PARTED_STEPS) narrows, its union equal to the
        # unparted output (ontology/tests/test_people.py PartedStepTest). with_owners
        # reaches none of the changed definitions; its v2 digest does not move
        ("111dcf4fa6374a312ac746ede3e0f55c32561d1b62e137caf735ea948fa1dfc3",
         "1b522413232efbc6a9cfce8ec6c0fb3c63909c4f86740c4f667be0b0fee619c2")}),
    # NW-13 too: the batched reads (test_step_reads); supply_pegging reads only
    # sender_parties' _digest, _q, _values and _versions, which NW-13 left as they were
    "sender_parties": frozenset({
        ("2b3effc22bd4b1a05839a1dde1ae5f4cbda4eb908a381136943f8934d84baff6",
         "90024d09ea422aba97701afbf3e5c175273790ecd5921c9976acc74f2083a93d")}),
    "unique_mentions": frozenset({
        ("31f2628ad7d359b774b0403161c5f32dd4d22d1733d14a67ab0672d144722969",
         "ab125ea7895e988607114d4939cb816caba104317a8df54e843d26bd9fcc3cb3"),
        # NW-13 (88fdc30dff): one same_record read per window of candidates; equal row for
        # row to the served step's applied crb-mid: rows on the served store (11-14) and to the per-object step in test_step_reads
        ("08d75715ea690c5c2adf69dc15098f0a80b844cd1ef027dfe29d983991a018cb",
         "90ab15b2dee3ab609a1d03cb764526e0eb5eb9ac830a8eacddfa27281dd327b0")}),
    "supply_pegging": frozenset({
        ("51d8ce8f082dae86258dcb6a589246933dc6184c56367e0d9b5df7f72e90b44a",
         "ae067f2d98c6c50fee42f7a9658120a97c9a6a137e3a2eba249a282bc72e8ac3")}),
    "twins": frozenset({
        ("131db39416cf5c60510416648e4679711ec956f989e287fc3311577473ef6d05",
         "8881e1c06adcc734fe71ded0903b5fabdc89ae7cec70376c723268567290de51")}),
    "your_reference": frozenset({
        ("131db39416cf5c60510416648e4679711ec956f989e287fc3311577473ef6d05",
         "8881e1c06adcc734fe71ded0903b5fabdc89ae7cec70376c723268567290de51")}),
    "with_owners": frozenset({
        ("a2887e8a79b92a6f292887258f83414460eceaed3b0bad5f37abf4f5aea9d14b",
         "f8b5571f8f1d35ccfe76cae434b2b602eb8e92e168b7ffb06d1af1fa63f8454e"),
        ("f8b5571f8f1d35ccfe76cae434b2b602eb8e92e168b7ffb06d1af1fa63f8454e",
         "5583b37cf462ccb7750b52d8e5b9de3b755fa945a44c469390171ba899c6b448")}),
    "document_fields": frozenset({
        ("ab0f767f19df54ad663aae34db67584c2ee1d25230104edea7735af56e68eb38",
         "b4256f70d305a06e1a7ee37bb9068ddb6f39bfe6fe720fe9342bc016d8ff1de2"),
        # blob-zstd (29ea163b53 onto train 3's c387ba7453): the held tests (_bytes_kept
        # through reading_kept, _bytes_held) and read_blob/stored_text/_may_contain see a
        # file, a packed row or the bytes' .zst where they saw a file. They differ only
        # for a form held packed or .zst and not as a file: none of the documents' blobs
        # on the served store (11-15), and every
        # .zst the compress pass writes holds the bytes the file held; the derivation,
        # readings and document codes keep c387ba7453's (ontology/code_equivalence.py)
        ("2b6d76170dedb80ddb2fb994f7a12ca5a8507ae791a0ef92d5ffac6b697d4e70",
         "42e1baeea91b73c1bf3d9071b667108c05b12570f315fff6591bd0a9cab7a848"),
        # the same change in the v2 scheme (CAP-L180): compare_code(c387ba7453) -> this
        # tree, whose v2 digests are PIN_4 f9bdbbbcb3's; the plan compares v2 on both sides
        ("10343bcdc3d9b30ddf9353b085321b60092c68f7a8b199b2cbf34564dd174268",
         "e5515c63986f66fb40d5da12f3e8ad7f815c0438fc55c3cd14fb5bf56f6c8b26")}),
    "confirmations": frozenset({
        ("fff623a24d1b3f3fb7ed8ba05247e25092474886446e5ceb6a3e35f3201d3efd",
         "3cfad85742ea1f054c91a43ccd0a56003a6ee321b079846a92b973e017e16224")}),
    # CC-C-04 (rewrite-d-1114, onto PIN_3 c387ba7453): mc_task_links.py moved only in
    # task_ids' code (firm_links/_firm_row/task_ids_step), which task_names never calls
    "task_names": frozenset({
        ("db4aea67eb592dbe2f8c65254acd78f3da26d05d037300f0d753fece1a60e8c9",
         "b5d7bc1727a61b0abf8b68ba57a9756c0c7331806bc270afdf194ff04e2abbfb"),
        # the same change in the v2 scheme (CAP-L180): compare_code(c387ba7453) -> this
        # tree, whose v2 digests are PIN_4 f9bdbbbcb3's; the plan compares v2 on both sides
        ("e5557647466ea90ecb8ad4a667638711a213c5bb5b2a0b0b6f1488074052cd58",
         "fe77ce5725e624240a754e0745a6ceaa89ed3162e8d6ba0ae32e54ddc34e4594")}),
}
# Code changes of a step proven to write, on a store ``operating.pg.mention_collapse`` folded
# (CC-C-04: a document's repeated mention rows of one link held as one row carrying its
# occurrences, ``ontology.mention_occurrences``), exactly the rows that collapse leaves, and
# on a store it never folded the rows it wrote before: (from, to) pairs of the step's code
# digest, each pinned by reading the change and by the collapse's proof (its ``verify`` and
# the occurrence-aware step re-run on a rehearsal copy). A step migration across one does not
# run the step again, nor its readers for it (as :data:`SAME_ROWS`); the plan names the step
# under ``"collapsed"`` and the migration runs the collapse (``mention_collapse run``, then
# ``verify``) in place of it. Filled at the train's PIN from the served step code.
COLLAPSED: dict[str, frozenset[tuple[str, str]]] = {
    # CC-C-04 onto PIN_3 c387ba7453 (c04_pins.py --from-ref c387ba7453, merge 5cb87c200f)
    "unique_mentions": frozenset({
        ("11cb2931ab6ab2f4e7c39e993abce57751dce855749648a78483bddc514c2f69",
         "cc42c6c5b5c3e53a8bf493d21e746a719847a5a1e1233a276af117528c740a00"),
        # the same change in the v2 scheme (CAP-L180): compare_code(c387ba7453) -> this
        # tree, whose v2 digests are PIN_4 f9bdbbbcb3's; the plan compares v2 on both sides
        ("57eb9068227262e8890040d3260bb487b1ba5bd0ce2976558035b876d687db5f",
         "9334e3f38d1a5d6e0b3e7407307dfc243ca327d7149c1508cdf2fccfbfb14320")}),
    "task_ids": frozenset({
        ("db4aea67eb592dbe2f8c65254acd78f3da26d05d037300f0d753fece1a60e8c9",
         "1d3bc77e26f482ea16f2ea776a23d395c2cf048e18a0433731455e6066336dc1"),
        # the same change in the v2 scheme (CAP-L180): compare_code(c387ba7453) -> this
        # tree, whose v2 digests are PIN_4 f9bdbbbcb3's; the plan compares v2 on both sides
        ("079903a8981e4ec436d4218a6443ac53864664638e4e5d00bb77a91a516ce703",
         "6c348054033e0388295a6d677d50dadb643f669aff9d75c35e07ab659f3b1996")}),
}
# The steps whose re-run on a store ``operating.pg.mention_collapse`` folded writes the rows it
# writes on the uncollapsed store (11-15). Every other step is
# refused there (``collapsed_store:<step>``): a step that reads ``mention:`` rows one at a time
# (as ``sender_corroboration`` did before it read the twins back: one row per occurrence) sees
# only the keepers and loses its rows for the folded twins, and a step that reads the moved
# ``crb-mid:``/``crb-mcid:`` rows or a keeper's reason sees other ids and text. Fail closed: a
# step is named here only on a proof (its code read, or a collapsed-vs-uncollapsed test).
COLLAPSE_SAFE: dict[str, str] = {
    # occurrence-aware: read every occurrence back (``ontology.mention_occurrences.views``)
    "unique_mentions": "occurrence_aware",
    "task_ids": "occurrence_aware",
    # reads every folded twin back with its own id (sender_corroboration.as_uncollapsed);
    # ontology/tests/test_sender_corroboration_collapsed.py: row for row the uncollapsed rows
    "sender_corroboration": "occurrence_aware",
    # read no link row at all (operating/kind_confirmation.py: its own SQLite only;
    # ontology/text_index.py: document texts only)
    "confirmations": "reads_no_links",
    "text_index": "reads_no_links",
}
COLLAPSE_REFUSAL = "collapsed_store:"


def collapse_refusals(run: Iterable[str], store_collapsed: bool,
                      safe: Mapping[str, str] = COLLAPSE_SAFE) -> list[str]:
    """``collapsed_store:<step>`` for each step of ``run`` not :data:`COLLAPSE_SAFE`, when the
    store is collapsed; nothing otherwise."""
    if not store_collapsed:
        return []
    return [f"{COLLAPSE_REFUSAL}{name}" for name in sorted(set(run)) if name not in safe]


# The collapse's own markers (``operating.pg.mention_collapse`` ``MARK``, ``RETIRED``, ``UNDO``;
# the guard's test holds them equal): read here, below ``operating`` in the layer contract.
COLLAPSE_MARK = "c04:"
COLLAPSE_RETIRED = "c04-retired"
COLLAPSE_UNDO = "ontology.mention_collapse_undo"


def store_collapsed(cur: Any) -> bool:
    """Whether ``operating.pg.mention_collapse`` folded (part of) the store at cursor ``cur``:
    a done batch's mark (``c04:<type>:<n>`` in ``ontology.load_state``: kept after
    ``retire``, dropped when the batch is undone whole), the ``c04-retired`` record, or a
    row in the undo table. An unreadable store raises (fail closed)."""
    cur.execute("SELECT to_regclass('ontology.load_state') IS NOT NULL, to_regclass(%s) "
                "IS NOT NULL", (COLLAPSE_UNDO,))
    has_state, has_undo = cur.fetchone()
    if has_state:
        cur.execute("SELECT EXISTS (SELECT 1 FROM ontology.load_state WHERE "
                    "starts_with(name, %s) OR name = %s)", (COLLAPSE_MARK, COLLAPSE_RETIRED))
        if cur.fetchone()[0]:
            return True
    if has_undo:
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {COLLAPSE_UNDO})")
        if cur.fetchone()[0]:
            return True
    return False


def collapse_refusal(cur: Any, run: Iterable[str]) -> str | None:
    """Why the steps ``run`` may not be re-run or applied on the store of cursor ``cur``
    (``collapsed_store:<step>,...`` when ``mention_collapse`` folded it and one of them is
    not :data:`COLLAPSE_SAFE`), else None. Raises when the store cannot be read."""
    run = sorted(set(run))
    if not [n for n in run if n not in COLLAPSE_SAFE]:
        return None
    found = collapse_refusals(run, store_collapsed(cur))
    return ",".join(found) or None


def refuse_on_collapsed(dsn: str, run: Iterable[str]) -> None:
    """:func:`collapse_refusal` on the store at ``dsn``, raised as ``ValueError``."""
    run = sorted(set(run))
    if not [n for n in run if n not in COLLAPSE_SAFE]:
        return
    import psycopg
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        why = collapse_refusal(cur, run)
    if why:
        raise ValueError(why)


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


def _tree_roots(names: Iterable[str]) -> dict[str, tuple[tuple[str, str | None], ...]] | None:
    """This tree's scheme for the steps ``names``: their roots (a step this tree's table
    does not root: none, each of its modules whole), or None (a v1 table: bytes)."""
    if ROOTS is None:
        return None
    return {name: ROOTS.get(name, ()) for name in names}


def code_digests(ref: str | None = None, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> dict[str, str]:
    """step -> digest of its code files at ``ref`` (None: this tree) under this tree's
    scheme (its table's roots: the definitions they reach, ``ontology.step_code``)."""
    files = {name: spec.code for name, spec in specs.items()}
    return step_code.code_digests(ref, repo, files, _tree_roots(files))


def byte_digests(ref: str | None = None, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> dict[str, str]:
    """step -> digest of its code files' bytes at ``ref`` (the v1 scheme, what a build of a
    v1 table recorded; the :data:`SAME_ROWS`/:data:`MARKS_ONLY` pins made before v2)."""
    return step_code.code_digests(ref, repo, {name: spec.code for name, spec in specs.items()})


def _legacy_files(ref: str, repo: str | Path) -> dict[str, tuple[str, ...]] | None:
    """step -> code files of a commit before the table, from its ``STEPS`` as written."""
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
    return files


def code_at(ref: str, repo: str | Path = ROOT) -> dict[str, str] | None:
    """step -> digest of its code files at ``ref``, as that commit's own table lists them
    (``ontology/step_code.json``, in its own scheme; before it, its ``STEPS`` as written
    in its ``ontology/step_migrate.py``, read without running it, by bytes): the code
    digests ``ref``'s build recorded, so ``steps_from`` is the digest the served store
    holds even after this tree adds a step or a code file to one, or roots its steps by
    definition. None: ``ref`` has no step migration. A table or ``STEPS`` entry that is
    not literal raises (never a guessed digest)."""
    tabled = step_code.code_at(ref, repo)
    if tabled is not None:
        return tabled
    files = _legacy_files(ref, repo)
    if files is None:
        return None
    return step_code.code_digests(ref, repo, files)


def recorded_code(ref: str, repo: str | Path = ROOT,
                  specs: Mapping[str, Spec] = STEPS) -> dict[str, str]:
    """The step code a build of ``ref`` recorded (:func:`code_at`; a commit older than
    step migrations: this tree's step files there, by bytes, what ``seed-steps`` held)."""
    held = code_at(ref, repo)
    return byte_digests(ref, repo, specs) if held is None else held


def recorded_steps_digest(ref: str, repo: str | Path = ROOT) -> str:
    """The steps digest a build of ``ref`` recorded (``seed-steps``, a load's
    ``--steps-ref``): :func:`recorded_code`, in ``ref``'s own scheme."""
    return step_code.steps_digest_of(recorded_code(ref, repo))


def compare_code(ref: str, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> dict[str, str]:
    """step -> digest of its code at ``ref`` under THIS tree's scheme, over ``ref``'s own
    step files (its table or ``STEPS``; none: this tree's): what a plan compares with this
    tree's :func:`code_digests`, step by step, so a v1 served commit and a v2 tree with the
    same definitions run nothing (``plan``'s ``steps_from`` stays the served digest)."""
    files = step_code.step_files(ref, repo)
    if files is None:
        files = _legacy_files(ref, repo)
    if files is None:
        return code_digests(ref, repo, specs)
    return step_code.code_digests(ref, repo, files, _tree_roots(files))


def steps_digest(ref: str | None = None, repo: str | Path = ROOT,
                 specs: Mapping[str, Spec] = STEPS) -> str:
    """The digest of every step's code (a delta layer records its own: ``steps_digest``)."""
    return step_code.steps_digest_of(code_digests(ref, repo, specs))


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
               same_rows: Mapping[str, Iterable[tuple[str, str]]] = SAME_ROWS,
               collapsed: Mapping[str, Iterable[tuple[str, str]]] | None = None
               ) -> dict[str, Any]:
    """The steps to run again (module docstring): ``{"run": {step: from_types | None},
    "refused": [...], "why": {step: reason}}``, and ``"remark": [...]`` when an
    unsupported step runs again only for a code change ``marks_only`` pins (its rows are
    re-marked in place, :data:`MARKS_ONLY`); a code change ``same_rows`` pins
    (:data:`SAME_ROWS`) is no reason to run a step again, nor one ``collapsed`` pins
    (:data:`COLLAPSED`: named under ``"collapsed"``, the migration folds the store in place
    instead). ``present``: the step names this tree's
    build runs. ``inputs``: the steps whose declared inputs the unit migration touches
    (``schema_migrate.diff``'s ``steps_required``: :func:`step_inputs`)."""
    present = set(present)
    run: dict[str, Any] = {}
    why: dict[str, str] = {}
    changed: dict[str, set[str]] = {}
    refused: list[str] = []
    inputs = dict(inputs or {})
    collapsed = COLLAPSED if collapsed is None else collapsed
    folded: list[str] = []
    for name in sorted(present & set(specs)):
        spec = specs[name]
        pair = (old_code.get(name), new_code.get(name))
        if pair[0] != pair[1] and pair in set(collapsed.get(name, ())):
            folded.append(name)   # the collapse stands for the run (``COLLAPSED``)
        elif pair[0] != pair[1] and pair not in set(same_rows.get(name, ())):
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
    if folded:
        out["collapsed"] = folded
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


def compare_pin(tree: str | Path, specs: Mapping[str, Spec] = STEPS) -> dict[str, Any]:
    """The ``--served-compare-code`` pin of a build-only served tree (a directory holding
    the served build's files, its ``ontology/step_code.json`` or ``STEPS``): its step code
    under THIS tree's scheme (:func:`compare_code`), with ``of``, the steps digest that
    tree's own build recorded (:func:`recorded_code`, which must be the ``--served-code``
    pin's ``steps_digest``: :func:`bound_compare_code`)."""
    code = compare_code(None, tree, specs)   # type: ignore[arg-type]
    return {"code": code, "steps_digest": step_code.steps_digest_of(code),
            "of": step_code.steps_digest_of(recorded_code(None, tree, specs))}  # type: ignore[arg-type]


def bound_compare_code(path: str | Path, served: Mapping[str, str]) -> dict[str, str]:
    """A ``--served-compare-code`` pin (:func:`compare_pin`), verified as :func:`served_code`
    and bound to the served code ``served`` (step -> digest in the served build's own
    scheme): its ``of`` is ``served``'s steps digest and it holds the same steps. Refused
    (``served_compare_code_unbound``) otherwise: a pin made from another tree is never
    compared."""
    code = served_code(path)
    pin = json.loads(Path(path).read_text(encoding="utf-8"))
    if pin.get("of") != step_code.steps_digest_of(served) or set(code) != set(served):
        raise ValueError("served_compare_code_unbound")
    return code


def plan(served_ref: str, new: Any = None, repo: str | Path = ROOT,
         specs: Mapping[str, Spec] = STEPS, present: Iterable[str] | None = None,
         old_code: Mapping[str, str] | None = None,
         compare: Mapping[str, str] | None = None,
         store_collapsed: bool = False) -> dict[str, Any]:
    """The whole migration from the code at ``served_ref`` to this tree's
    (:func:`plan_schemas` with the served commit's schema and step code). ``old_code``:
    the served step code when no commit holds it (:func:`served_code`); the schema still
    comes from ``served_ref``.

    Two digests of the served step code: the plan's ``steps_from`` is the one the served
    build recorded, in its own scheme (:func:`recorded_code`, or ``old_code``), the digest
    the served store holds (an apply refuses ``other_steps`` otherwise); each step is
    compared with this tree's under this tree's scheme on both sides (:func:`compare_code`,
    or ``compare``: a pin of the build-only served tree's code in this tree's scheme). The
    first plan from a byte-scheme (v1) served commit to a v2 tree with the same
    definitions therefore runs nothing and only moves the held digest. A pinned
    ``old_code`` over a v1 served commit compares only with its ``compare`` pin (its
    digests are bytes): refused (``served_compare_code_needed``) without it."""
    from ontology.schema import load_schema
    from ontology import schema_migrate

    new = load_schema() if new is None else new
    served = schema_migrate.schema_at(served_ref, repo)
    recorded = recorded_code(served_ref, repo, specs) if old_code is None else dict(old_code)
    if compare is None:
        if old_code is None:
            compare = compare_code(served_ref, repo, specs)
        elif ROOTS is not None and step_code.step_roots(served_ref, repo) is None:
            raise ValueError("served_compare_code_needed")
        else:
            compare = old_code
    new_code = code_digests(None, repo, specs)
    grammar = grammar_changes(_grammar(served_ref, Path(repo)), _grammar(None, Path(repo)))
    return plan_schemas(served, new, compare, new_code, present, specs, grammar=grammar,
                        steps_from=step_code.steps_digest_of(recorded),
                        store_collapsed=store_collapsed)


# What of the identifier grammar (``ontology/schema/identifiers.json``) decides no stored
# row: the words written for a reader.
_GRAMMAR_PROSE = frozenset({"description", "upstream", "why", "example"})
# Its sections only the mentions step reads (its code: a change re-runs it, ``Spec.code``).
_GRAMMAR_MENTIONS_ONLY = frozenset({"mention_profiles"})
# A kind's keys only one step reads, never a projection unit's parse: ``firm_mention``
# (which record a mention of the kind is raised to, ``identifiers.firm_types``/``firm_shape``)
# is unique_mentions' (``mention_firm``). A change moves no unit; it re-runs that step
# (``grammar_changes`` ``steps``, :func:`plan_schemas`).
_GRAMMAR_KIND_STEP_ONLY = {"firm_mention": "unique_mentions"}


def _grammar(ref: str | None, repo: Path) -> dict[str, Any]:
    data = _read("ontology/schema/identifiers.json", ref, repo)
    if data is None:
        return {}
    grammar = json.loads(data)
    if not isinstance(grammar, dict) or not isinstance(grammar.get("kinds", {}), dict):
        raise ValueError(f"identifier_grammar_unreadable:{ref or 'tree'}")
    return grammar


def _rows_part(item: Any) -> Any:
    if isinstance(item, Mapping):
        return {k: _rows_part(v) for k, v in item.items() if k not in _GRAMMAR_PROSE}
    if isinstance(item, list):
        return [_rows_part(v) for v in item]
    return item


def grammar_changes(served: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, list[str]]:
    """What moved in the identifier grammar between the served tree and this one, prose
    aside: ``kinds`` (a kind whose pattern, normalizer, length or any other rule changed,
    added or gone) and ``sections`` (any other top-level section but the mentions step's
    own). A kind's parse decides the identifier rows a projection unit writes for the
    properties carrying it, which no schema slice holds (CT-D-09: a looser Message-ID
    pattern left 291,569 served Email identifier rows unwritten under the old grammar)."""
    def parse(spec: Any) -> Any:
        part = _rows_part(spec)
        return {k: v for k, v in part.items() if k not in _GRAMMAR_KIND_STEP_ONLY} \
            if isinstance(part, Mapping) else part
    kinds_a, kinds_b = served.get("kinds") or {}, new.get("kinds") or {}
    kinds = sorted(k for k in set(kinds_a) | set(kinds_b)
                   if parse(kinds_a.get(k)) != parse(kinds_b.get(k)))
    sections = sorted(k for k in set(served) | set(new)
                      if k != "kinds" and k not in _GRAMMAR_MENTIONS_ONLY | _GRAMMAR_PROSE
                      and _rows_part(served.get(k)) != _rows_part(new.get(k)))

    def rest(spec: Any) -> Any:
        return {k: v for k, v in (parse(spec) or {}).items() if k not in _WIDENING}
    # a kind whose only change is what text it accepts (its pattern, its length): every
    # served row of it is re-read equal or the phase that relies on it refuses
    # (``operating.pg.identifier_reparse check``)
    widened = sorted(k for k in kinds if k in kinds_a and k in kinds_b
                     and rest(kinds_a[k]) == rest(kinds_b[k]))
    out = {"kinds": kinds, "sections": sections, "widened": widened}
    steps = sorted({step for key, step in _GRAMMAR_KIND_STEP_ONLY.items()
                    for k in set(kinds_a) | set(kinds_b)
                    if _rows_part((kinds_a.get(k) or {}).get(key))
                    != _rows_part((kinds_b.get(k) or {}).get(key))})
    if steps:
        out["steps"] = steps
    return out


# What a kind's change may touch for its served rows to be completed in place, set-based
# (``reparse_carriers``): the text it accepts, never its normalizer, forms or identity.
_WIDENING = frozenset({"value_pattern", "max_length"})
# Sources whose records the in-place re-parse reads by handle
# (``operating.pg.identifier_reparse``): a carrier of another source is re-projected.
REPARSE_SOURCES = frozenset({"graph_mail"})


def reparse_carriers(new: Any, kind: str) -> list[dict[str, Any]] | None:
    """The carriers of ``kind`` whose only rows a parse of it decides are their own value
    cell and identifier row (the projector's ``_value``/``_project_values``), so a kind
    that only accepts more text adds rows for the records it newly parses and changes no
    other row: a set-based phase (``operating.pg.identifier_reparse``) instead of
    re-projecting their units. None (re-project them) when any link compares by the kind,
    a mention profile reads it from text or another kind aliases it,
    another property names a carrier (placeholders, eras), an event type of the type
    reads it, a carrier has an authority precedence, its binding has marking rules (a
    version's own markings) or reads a source the phase cannot read by handle."""
    types = _get(new, "object_types") or {}
    links = _get(new, "link_types") or {}
    events = _get(new, "event_types") or {}

    def names(item: Any, value: str) -> bool:
        if isinstance(item, Mapping):
            return any(names(v, value) for v in item.values())
        if isinstance(item, (list, tuple)):
            return any(names(v, value) for v in item)
        return item == value
    if any(names(link, kind) for link in links.values()):
        return None
    from ontology import identifiers
    grammar = json.loads(identifiers.SCHEMA_PATH.read_text(encoding="utf-8"))
    if names(grammar.get("mention_profiles"), kind) or any(
            kind in (spec.get("alias_kinds") or ()) for spec in grammar["kinds"].values()):
        return None   # read from text: the mention scope would have to cover its new keys
    out = []
    for tid, t in sorted(types.items()):
        for prop in t.get("properties", ()):
            if prop.get("identifier_kind") != kind:
                continue
            if prop.get("authority") or prop.get("availability") == "not_served":
                return None
            others = [p for p in t.get("properties", ()) if p is not prop]
            if any(names(p.get("placeholder_when_equal_to"), prop["name"])
                   or names(p.get("authority"), prop["name"]) for p in others):
                return None
            if any(ev.get("object_type") == tid and names(ev, prop["name"])
                   for ev in events.values()):
                return None
            unique = any(prop["name"] in key.get("properties", ()) and key.get("unique")
                         for key in t.get("identity_keys", ()))
            for src in prop.get("sources", ()):
                b = next((b for b in t.get("bindings", ()) if b["id"] == src["binding"]),
                         None)
                if (b is None or src.get("parser") != "identifier" or src.get("when")
                        or b.get("marking_rules") or b["source"] not in REPARSE_SOURCES):
                    return None
                out.append({"kind": kind, "type": tid, "property": prop["name"],
                            "binding": b["id"], "relation": b["relation"],
                            "source": b["source"], "path": src["path"],
                            "role": "primary" if unique else "alias",
                            "markings": sorted(set(t.get("markings", ()))
                                               | set(prop.get("markings", ())))})
    return out or None


# A timestamp source's declared zone (C-09, ``project._timestamp``): the only keys a
# zone-only change may add for its served rows to be rewritten in place, set-based
# (``operating.pg.timestamp_rezone``) instead of re-projecting their units.
ZONE_KEYS = ("naive_zone", "wall_clock_zone")


def _unzoned(spec: Any) -> Any:
    return {k: v for k, v in spec.items() if k not in ZONE_KEYS} \
        if isinstance(spec, Mapping) else spec


def _zone_of(spec: Mapping[str, Any]) -> dict[str, str]:
    return {k: spec[k] for k in ZONE_KEYS if k in spec}


def rezone_carriers(served: Any, new: Any) -> dict[str, Any]:
    """The timestamp sources and event times whose only change from ``served`` to ``new``
    is a zone declared where none was (C-09): their served rows change in nothing but the
    text of a timestamp, which ``operating.pg.timestamp_rezone`` rewrites in Postgres from
    the stored text (``project._timestamp`` of it under the new zone), so their units are
    not rewritten.

    ``{"values": [...], "events": [...], "served": <served with those zones declared>}``:
    ``served`` is what the unit diff compares against (its digest stays the served one),
    so a unit whose slice differs only by them keeps its rows. A source is a carrier only
    when its parse decides nothing but its own value cell: an ``iso_timestamp`` source of
    a ``timestamp`` property with no ``when``, no identifier kind, in no identity key, no
    other property of its type naming it (authority, placeholder), no link type touching
    its type naming it (a pair, a compared field), no capture-observed event observing it,
    and the source declared no zone before. Anything else stays in the unit diff (its
    units are rewritten), never guessed."""
    types_a = dict(_get(served, "object_types") or {})
    types_b = dict(_get(new, "object_types") or {})
    links = _get(new, "link_types") or {}
    events_a = dict(_get(served, "event_types") or {})
    events_b = dict(_get(new, "event_types") or {})

    def names(item: Any, value: str) -> bool:
        if isinstance(item, Mapping):
            return any(names(v, value) for v in item.values())
        if isinstance(item, (list, tuple)):
            return any(names(v, value) for v in item)
        return item == value

    bindings = _get(new, "bindings") or {}

    def relation(bid: str) -> str | None:
        b = bindings.get(bid)
        return b[1].get("relation") if b else None
    values: list[dict[str, Any]] = []
    patched_types: dict[str, Any] = {}
    for tid in sorted(set(types_a) & set(types_b)):
        ta, tb = types_a[tid], types_b[tid]
        props_a = {p["name"]: p for p in ta.get("properties", ())}
        new_props = []
        changed = False
        for pb in tb.get("properties", ()):
            pa = props_a.get(pb["name"])
            if pa is None or pa == pb:
                new_props.append(pa if pa is not None else pb)
                continue
            srcs_a, srcs_b = list(pa.get("sources", ())), list(pb.get("sources", ()))
            same_but_zone = (
                {k: v for k, v in pa.items() if k != "sources"}
                == {k: v for k, v in pb.items() if k != "sources"}
                and len(srcs_a) == len(srcs_b)
                and all(_unzoned(a) == _unzoned(b) for a, b in zip(srcs_a, srcs_b)))
            zoned = [(a, b) for a, b in zip(srcs_a, srcs_b) if a != b]
            eligible = (
                same_but_zone and zoned
                and pb.get("value_type") == "timestamp" and not pb.get("identifier_kind")
                and all(not _zone_of(a) and _zone_of(b) and b.get("parser") == "iso_timestamp"
                        and not b.get("when") for a, b in zoned)
                and not any(pb["name"] in key.get("properties", ())
                            for key in tb.get("identity_keys", ()))
                and not any(names(p.get("authority"), pb["name"])
                            or names(p.get("placeholder_when_equal_to"), pb["name"])
                            for p in tb.get("properties", ()) if p is not pb)
                and not any(tid in (link.get("from"), link.get("to"))
                            and names(link.get("bindings"), pb["name"])
                            for link in links.values())
                and not any(ev.get("object_type") == tid
                            and ev.get("observed_property") == pb["name"]
                            for ev in events_b.values())
                # a same_record link between two records of the type compares every
                # property both ends hold (``Projector._conflict``): its rows could move
                and not any(lb.get("kind") == "same_record"
                            and {lb.get("from_binding"), lb.get("to_binding")}
                            <= {s.get("binding") for s in srcs_b}
                            for link in links.values()
                            if link.get("from") == tid and link.get("to") == tid
                            for lb in link.get("bindings", ())))
            if not eligible:
                new_props.append(pa)
                continue
            changed = True
            new_props.append(pb)   # the served schema as if it had declared the zone
            for _, b in zoned:
                values.append({"type": tid, "property": pb["name"], "binding": b["binding"],
                               "relation": relation(b["binding"]), "path": b["path"],
                               "zone": _zone_of(b)})
        if changed:
            patched_types[tid] = {**ta, "properties": new_props}
    events: list[dict[str, Any]] = []
    patched_events: dict[str, Any] = {}
    for eid in sorted(set(events_a) & set(events_b)):
        ea, eb = events_a[eid], events_b[eid]
        if ea == eb:
            continue
        ta, tb = ea.get("time") or {}, eb.get("time") or {}
        if ({k: v for k, v in ea.items() if k != "time"}
                == {k: v for k, v in eb.items() if k != "time"}
                and _unzoned(ta) == _unzoned(tb) and not _zone_of(ta) and _zone_of(tb)
                and tb.get("parser", "iso_timestamp") == "iso_timestamp"
                and eb.get("time_basis") != "capture_observed"):
            events.append({"event_type": eid, "type": eb.get("object_type"),
                           "binding": tb["binding"], "relation": relation(tb["binding"]),
                           "path": tb["path"], "parser": tb.get("parser"),
                           "zone": _zone_of(tb)})
            patched_events[eid] = eb
    patched = served
    if patched_types or patched_events:
        patched = dataclasses.replace(
            served, object_types={**types_a, **patched_types},
            event_types={**events_a, **patched_events}) \
            if dataclasses.is_dataclass(served) else {
                **served, "object_types": {**types_a, **patched_types},
                "event_types": {**events_a, **patched_events}}
    return {"values": values, "events": events, "served": patched}


def _identifier_units(new: Any, kinds: Iterable[str]) -> list[str]:
    """The units whose rows a change of ``kinds`` moves: every binding (or shard) of a type
    with a property carrying one (its identifier rows), and every link unit of a link type
    that compares by one (its keys)."""
    from ontology.project_incremental import plan as unit_plan
    kinds = set(kinds)
    if not kinds:
        return []
    the_plan = unit_plan(new)
    types = {tid for tid, t in (_get(new, "object_types") or {}).items()
             if any(p.get("identifier_kind") in kinds for p in t.get("properties", ()))}
    bindings = {b["id"] for tid in types
                for b in ((_get(new, "object_types") or {})[tid].get("bindings", ()))}

    def compares(item: Any) -> bool:
        if isinstance(item, Mapping):
            return item.get("identifier_kind") in kinds or any(
                compares(v) for v in item.values())
        if isinstance(item, (list, tuple)):
            return any(compares(v) for v in item)
        return False
    links = {lid for lid, link in (_get(new, "link_types") or {}).items() if compares(link)}
    return sorted(u for u, row in the_plan.rows.items()
                  if (row[0] in ("binding", "shard") and row[1] in bindings)
                  or (row[0] == "links" and row[1] in links))


def present_steps() -> list[str]:
    """The step names this tree's build runs (``operating.build_steps.post_project``)."""
    from operating.build_steps import post_project
    return sorted(post_project(Path("/nonexistent-state"), "plx"))


def plan_schemas(served: Any, new: Any, old_code: Mapping[str, str],
                 new_code: Mapping[str, str], present: Iterable[str] | None = None,
                 specs: Mapping[str, Spec] = STEPS,
                 grammar: Mapping[str, Sequence[str]] | None = None,
                 steps_from: str | None = None,
                 store_collapsed: bool = False) -> dict[str, Any]:
    """The whole migration from ``served`` (its step code ``old_code``) to ``new``
    (``new_code``): the unit migration (``ontology.schema_migrate.diff``, its step link and
    step input refusals left to the step migration, the document-field bindings forced
    when their derivation changed) and the step migration (:func:`plan_steps`: the steps
    whose code, written link types or declared inputs changed, and their readers).
    ``grammar``: :func:`grammar_changes`; the units its changed kinds move are forced
    (:func:`_identifier_units`), and a changed section no unit rule covers is refused
    (``identifier_grammar_changed:<section>``), never left as served rows.
    ``steps_from``: the digest the served store holds when ``old_code`` is the served
    code in another scheme than the one it recorded (:func:`plan`); default its own.
    ``store_collapsed``: the served store is one ``operating.pg.mention_collapse`` folded;
    each step run again that is not :data:`COLLAPSE_SAFE` is refused
    (``collapsed_store:<step>``)."""
    from ontology import schema_migrate

    present = sorted(present_steps() if present is None else present)
    first = plan_steps(served, new, old_code, new_code, present, specs)
    force = []
    if "document_fields" in first["run"] and first["why"]["document_fields"] == "code":
        force = _document_field_units(new)
    grammar = grammar or {}
    reparse = {k: c for k in grammar.get("widened", ())
               if (c := reparse_carriers(new, k)) is not None}
    forced_kinds = [k for k in grammar.get("kinds", ()) if k not in reparse]
    force = sorted(set(force) | set(_identifier_units(new, forced_kinds)))
    touch = sorted({c["type"] for carriers in reparse.values() for c in carriers})
    # C-09: a zone declared on a timestamp source rewrites its served text in place
    # (``operating.pg.timestamp_rezone``); the unit diff sees the zone as served
    rezone = rezone_carriers(served, new)
    touch = sorted(set(touch) | {c["type"] for c in rezone["values"] + rezone["events"]})
    units = schema_migrate.diff(rezone["served"], new, force=force, present=present,
                                specs=specs, touch=touch)
    # a carrier whose binding unit is rewritten anyway is parsed by the projection
    rewritten = {u[2:].split("#")[0] for u in units.get("units", {}).get("affected", ())
                 if u.startswith("b:")}
    units["identifier_reparse"] = [c for k in sorted(reparse) for c in reparse[k]
                                   if c["binding"] not in rewritten]
    units["timestamp_rezone"] = {
        "values": [c for c in rezone["values"] if c["binding"] not in rewritten],
        "events": [c for c in rezone["events"] if c["binding"] not in rewritten]}
    # their objects are in the migration's scope: one a delta of the served schema writes
    # after the snapshot is projected again under this one (``ontology_migrate._newer``)
    units["scope"] = sorted(set(units.get("scope") or ()) | {
        f"{c['type']}:{c['binding']}:" for c in units["identifier_reparse"]} | {
        f"{c['type']}:{c['binding']}:" for part in units["timestamp_rezone"].values()
        for c in part})
    steps = plan_steps(served, new, old_code, new_code, present, specs,
                       inputs=units.get("steps_required"))
    for name in grammar.get("steps", ()):   # a grammar key only that step reads moved
        if name in present and name in specs and name not in steps["run"]:
            steps["run"][name] = None
            steps["why"][name] = "identifier_grammar"
    if store_collapsed:   # a store the collapse folded: only occurrence-safe steps re-run
        steps["refused"] = list(steps["refused"]) + collapse_refusals(steps["run"], True)
        steps["store_collapsed"] = True
    covered = set()
    for r in units["refused"]:
        if r.startswith("step_link_type:"):
            lid = r.split(":")[1]
            if any(lid in _written(s, specs[n]) for n in steps["run"] for s in (served, new)):
                covered.add(r)
        elif r.startswith("step_input_changed:") and r.split(":")[1] in steps["run"]:
            covered.add(r)
    units["refused"] = [r for r in units["refused"] if r not in covered] + [
        f"identifier_grammar_changed:{section}" for section in grammar.get("sections", ())]
    units["identifier_kinds_changed"] = list(grammar.get("kinds", ()))
    out = {"format": FORMAT, "units": units, "steps": steps,
           "steps_from": steps_from or "steps:" + _sha(old_code)[:32],
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
    from ontology.store import main_not_imported
    if job.__module__ == "__main__":
        # ``python -m`` runs this file as ``__main__``: a worker imports nothing of the
        # build's main (main_not_imported), so the job is this module's own, by its import
        # name (static attributes of the three jobs, never a computed lookup)
        from ontology import step_migrate as canonical
        job = {"_served_value_part": canonical._served_value_part,
               "_layer_value_part": canonical._layer_value_part,
               "_layer_link_part": canonical._layer_link_part}[job.__name__]
    with main_not_imported():
        pool = cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                      **parent_watch.pool_init())
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


_HELD_KEY_CHUNK = 200_000


def _copy_held_keys(cur: Any, conn: sqlite3.Connection, drop_types: Sequence[str],
                    drop_prefixes: Sequence[str], drop_links: Sequence[Sequence[str]]) -> None:
    """Copy into ``conn`` the keys of the served values, identifiers and links of the
    objects outside the temporary ``inp`` (the declared types' objects), leaving out
    what the unit migration deletes.

    Chunks ``outp`` in ``_HELD_KEY_CHUNK``-row slices joining the compact tables
    (``object_versions -> value_sources -> value_cells -> value_fields`` and
    ``identifiers``) and queries ``ontology.link_rows`` directly (no self-join to
    ``ontology.links``) so large stores never spill temp files on Postgres."""
    _held_tables(conn, indexes=False)   # indexed once filled: far fewer page writes
    cur.execute(
        "CREATE TEMP TABLE outp ("
        "rn bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
        "object_id text NOT NULL UNIQUE)"
    )
    cur.execute(
        "INSERT INTO outp (object_id) "
        "SELECT o.object_id FROM ontology.objects o WHERE NOT EXISTS "
        "(SELECT 1 FROM inp WHERE inp.object_id = o.object_id) AND NOT EXISTS "
        "(SELECT 1 FROM unnest(%s::text[], %s::text[]) d(t, p) WHERE o.object_type = d.t "
        "AND starts_with(o.object_id, d.p))",
        (drop_types, drop_prefixes),
    )
    cur.execute("ANALYZE outp")
    cur.execute("SELECT coalesce(max(rn), 0) FROM outp")
    max_rn = int(cur.fetchone()[0] or 0)

    val_cols = next(c for _v, t, c, _k in HELD if t == "held_values")
    id_cols = next(c for _v, t, c, _k in HELD if t == "held_identifiers")
    link_cols = next(c for _v, t, c, _k in HELD if t == "held_links")

    val_sql = (
        "SELECT DISTINCT ov.object_id, vf.property "
        "FROM outp o "
        "JOIN ontology.object_versions ov ON ov.object_id = o.object_id "
        "JOIN ontology.value_sources vs ON vs.ver = ov.ver "
        "JOIN ontology.value_cells vc ON vc.src = vs.src "
        "JOIN ontology.value_fields vf ON vf.field = vc.field "
        "WHERE o.rn BETWEEN %s AND %s"
    )
    id_sql = (
        "SELECT DISTINCT t.identifier_kind, t.normalized, t.object_id, t.property "
        "FROM outp o "
        "JOIN ontology.identifiers t ON t.object_id = o.object_id "
        "WHERE o.rn BETWEEN %s AND %s"
    )
    for lo in range(1, max_rn + 1, _HELD_KEY_CHUNK):
        hi = lo + _HELD_KEY_CHUNK - 1
        with cur.connection.cursor(name=f"held_values_{lo}") as named:
            for batch in _rows(named, val_sql, (lo, hi)):
                conn.executemany(
                    f"INSERT INTO held_values VALUES ({', '.join('?' * len(val_cols))})",
                    [tuple(r[c] for c in val_cols) for r in batch],
                )
        with cur.connection.cursor(name=f"held_identifiers_{lo}") as named:
            for batch in _rows(named, id_sql, (lo, hi)):
                conn.executemany(
                    f"INSERT INTO held_identifiers VALUES ({', '.join('?' * len(id_cols))})",
                    [tuple(r[c] for c in id_cols) for r in batch],
                )

    link_sql = (
        "SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
        "l.binding_kind, l.superseded_at FROM ontology.link_rows l "
        "WHERE NOT EXISTS (SELECT 1 FROM inp WHERE inp.object_id = l.from_object) "
        "AND NOT EXISTS (SELECT 1 FROM inp WHERE inp.object_id = l.to_object) "
        "AND NOT EXISTS (SELECT 1 FROM "
        "unnest(%s::text[], %s::text[], %s::text[], %s::text[]) d(t, k, f, o) WHERE "
        "l.link_type = d.t AND l.binding_kind = d.k AND starts_with(l.from_object, d.f) "
        "AND starts_with(l.to_object, d.o))"
    )
    link_params = tuple([x[i] for x in drop_links] for i in range(4))
    with cur.connection.cursor(name="held_links") as named:
        for batch in _rows(named, link_sql, link_params):
            conn.executemany(
                f"INSERT INTO held_links VALUES ({', '.join('?' * len(link_cols))})",
                [tuple(r[c] for c in link_cols) for r in batch],
            )
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
    store_ddl.planner_stats(conn)
    conn.commit()


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
    parent_watch.die_with(parent_watch.spawned_parent())   # never outlives the build
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


def document_families(run: Mapping[str, Sequence[str] | None], new: Any,
                      specs: Mapping[str, Spec] = STEPS, *, every: bool = False) -> set[str]:
    """The source families the document steps of ``run`` read (:func:`_sources_of`: the
    sources bound to a step's from-types; None: every bound source), the Portal mirror
    left out (a state directory never holds it; the steps read only capture stores).
    ``every``: every bound capture family (the text index re-mark reads all documents)."""
    bound = lambda types: {b["source"] for t in types for b in  # noqa: E731
                           ((_get(new, "object_types") or {}).get(t) or {}).get("bindings", ())}
    every_type = list(_get(new, "object_types") or {})
    out: set[str] = set(bound(every_type)) if every else set()
    for name, from_types in run.items():
        spec = specs.get(name)
        if spec is not None and spec.documents:
            out |= bound(every_type if from_types is None else from_types)
    out.discard("portal_api")
    return out


def require_state_sources(run: Mapping[str, Sequence[str] | None], new: Any,
                          sources: Mapping[str, Any], served: Iterable[str] | None, *,
                          specs: Mapping[str, Spec] = STEPS, every: bool = False
                          ) -> list[str]:
    """``schema_migrate.require_sources`` for the document steps of ``run`` over a state
    directory's ``sources`` (SCHEMA-001): a family the served generation read
    (``served``: ``schema_migrate.served_families``) and the state directory does not hold
    refuses the build. The families never served."""
    from ontology.schema_migrate import require_sources
    wanted = document_families(run, new, specs, every=every)
    if not wanted:
        return []
    return require_sources(wanted, sources, served)


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
                      # the source families the document steps read (SCHEMA-001)
                      "sources": sorted(document_families(run, new, specs) & set(sources)),
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
    from ontology.schema_migrate import served_families
    from operating.pg.ontology_migrate import delta_mark

    mark = delta_mark(dsn)   # before the snapshot: anything later is queued at the apply
    projection = state / "projection"
    with contextlib.ExitStack() as stack:
        sources = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
        never = require_state_sources(the_plan["steps"]["run"], new, sources,
                                      served_families(dsn))
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
        report["sources"] = sorted(sources)
        report["sources_never_served"] = never
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.step_migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="the unit and step migration from a commit's code")
    p.add_argument("--from-ref", required=True)
    p.add_argument("--served-code", default=None,
                   help="the served step code pin (code + steps_digest) when no commit holds it")
    p.add_argument("--store-collapsed", choices=("yes", "no"), default=None,
                   help="the served store is one mention_collapse folded (refuses each step "
                        "re-run not COLLAPSE_SAFE); read from --dsn when not given")
    p.add_argument("--dsn", default=None,
                   help="read whether the served store is collapsed (no writes)")
    p.add_argument("--served-compare-code", default=None,
                   help="that code in this tree's scheme (a pin of the same form, from "
                        "compare-pin), compared step by step")
    c = sub.add_parser("compare-pin", help="the --served-compare-code pin of a build-only "
                                           "served tree (a directory)")
    c.add_argument("--tree", required=True)
    b = sub.add_parser("build", help="the step migration layer over the served ontology")
    b.add_argument("--from-ref", required=True)
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--unit-layer", default=None,
                   help="the unit migration layer applied in the same transaction")
    b.add_argument("--dsn", default=None)
    b.add_argument("--served-code", default=None,
                   help="the served step code pin (code + steps_digest) when no commit holds it")
    b.add_argument("--served-compare-code", default=None,
                   help="that code in this tree's scheme (a pin of the same form, from "
                        "compare-pin), compared step by step")
    args = parser.parse_args(argv)
    if args.command == "compare-pin":
        print(json.dumps(compare_pin(args.tree), indent=1, sort_keys=True))
        return 0
    old = served_code(args.served_code) if args.served_code else None
    compare = None
    if args.served_compare_code:
        compare = bound_compare_code(args.served_compare_code, old if old is not None
                                     else recorded_code(args.from_ref))
    collapsed = False
    if args.command == "plan" and args.store_collapsed is not None:
        collapsed = args.store_collapsed == "yes"
    elif args.command == "plan" and args.dsn:
        import psycopg
        with psycopg.connect(args.dsn) as pg, pg.cursor() as cur:
            collapsed = store_collapsed(cur)
    the_plan = plan(args.from_ref, old_code=old, compare=compare, store_collapsed=collapsed)
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
