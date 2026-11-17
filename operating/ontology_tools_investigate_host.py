"""What a host wires into ``plx.investigate``: the plan menu and tool specs, the
citation gate's rows, the reader's read scope, and the capture and relation
completeness claims.

Split from ``operating/ontology_tools_investigate.py`` (``register`` and
``citation_rows`` stay there); moved unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from ontology import store_ddl
from ontology.types import Actor
from .ontology_tools_policy import _AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES
from .ontology_tools_results import _inline_defs
from .ontology_tools_investigate_gaps import _binding_of
from .ontology_tools_investigate_plans import PLAN_DIR, Plan, TOOL_NAMES
from .ontology_tools_investigate_pages import _superseded_by


# =========================================================================== host wiring

_MENU_TITLES: dict[str, tuple[tuple, dict[str, str]]] = {}   # plan dir -> (file stamps, titles)
_MENU_LOCK = threading.Lock()


def plan_menu(plans: Mapping[str, Plan] | None = None,
              directory: str | Path = PLAN_DIR) -> str:
    """The families a caller can ask, each with the question it answers (the plan's
    ``title``), for the listed ``family`` argument: without it a client guesses names and
    gets ``no_plan``. ``plans``: the host's own; else the plan files' ids and titles, read
    without validating (a broken file is left out here and refused when called)."""
    titles: dict[str, str] = {}
    if plans is not None:
        titles = {pid: str(plan.doc.get("title") or "") for pid, plan in plans.items()}
    else:
        # read again only when a plan file changed (each host's tool table is built anew
        # per store state: reading every plan file was 7 s of a cold Ask under load)
        files = sorted(p for p in Path(directory).glob("*.json") if not p.name.startswith("_"))
        stamp = []
        for path in files:
            try:
                found = path.stat()
                stamp.append((path.name, found.st_mtime_ns, found.st_size))
            except OSError:
                stamp.append((path.name, None, None))
        key = (str(Path(directory).resolve()), tuple(stamp))
        with _MENU_LOCK:
            held = _MENU_TITLES.get(key[0])
        if held is not None and held[0] == key[1]:
            titles = dict(held[1])
        else:
            for path in files:
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(doc, Mapping) and isinstance(doc.get("id"), str):
                    titles[doc["id"]] = str(doc.get("title") or "")
            with _MENU_LOCK:
                _MENU_TITLES[key[0]] = (key[1], dict(titles))
    if not titles:
        return ""
    return ("Families (family: what its plan answers): "
            + "; ".join(f"{pid}: {title[:120]}" for pid, title in sorted(titles.items()))
            + ".")


def tool_specs(contracts: Mapping[str, Any], *, follow_ups: bool = False,
               plans: Mapping[str, Plan] | None = None) -> list[dict[str, Any]]:
    """The listed specs. ``follow_ups``: the host records the option (a recorder): the tool
    then writes case follow-ups and is listed as a writer, as the contract's ``read_only``
    false says; without one it writes nothing (the option is refused) and is listed
    read-only, so an auto-approving client never writes case records unconfirmed. The
    ``family`` argument's description lists the families (``plan_menu``; ``plans``: the
    host's own), which keeps the tool description within the listing's length cap."""
    menu = plan_menu(plans)
    specs = []
    for name in TOOL_NAMES:
        schema = _inline_defs(contracts["tools"][name]["input"], contracts)
        family = (schema.get("properties") or {}).get("family")
        if menu and isinstance(family, Mapping):   # copies: the contract stays unchanged
            schema["properties"] = {**schema["properties"],
                                    "family": {**family, "description": menu}}
        specs.append({"name": name, "description": contracts["tools"][name]["description"],
                      "inputSchema": schema,
                      "annotations": ({"readOnlyHint": False, "destructiveHint": False}
                                      if follow_ups else {"readOnlyHint": True})})
    return specs


def _with_captured_row(runtime: Any, view: Any, handle: str, actor: Actor) -> Any:
    """``view`` (``claim_row``'s, a captured SharePoint version, page or member) with the
    row ``plx.read`` serves this reader for it now (its fields and ``row.content_text``,
    read through the host's capture gate, ``CaseCaptures.served_row``), so a value a
    claim reads from what the reader was shown (a file's name, a passage of its text) is
    checked against exactly that; a page's view also carries that page's text as
    ``text`` (``PAGE_TEXT``). Only the claim gate's rows get it: ``claim_row``'s
    view is the one a case digests and stays as it is, and a field the view already
    states (a pinned file's ``fields``) is kept. A changed-since view, another scheme
    or a row the gate does not serve: ``view`` unchanged."""
    if isinstance(handle, str) and handle.startswith("graph-mail://") \
            and isinstance(view, Mapping) and not view.get("changed_since"):
        return _with_attachment_text(runtime, view, handle, actor)
    reader = getattr(getattr(runtime, "captures", None), "served_row", None)
    if not (callable(reader) and isinstance(view, Mapping) and isinstance(handle, str)
            and handle.startswith("sharepoint://")) or view.get("changed_since"):
        return view
    served = reader(handle, actor)
    if not isinstance(served, Mapping) or not served:
        return view
    row = view.get("row") if isinstance(view.get("row"), Mapping) else {}
    out = {**view, "row": {**served, **row}}
    if isinstance(view.get("page"), int) and isinstance(served.get("content_text"), str) \
            and "text" not in view:
        # A page's value is read on that page's text (field ``text``), as an original
        # page's and the scorer's re-read of the page give it.
        out["text"] = served["content_text"]
    return out


def _with_attachment_text(runtime: Any, view: Mapping[str, Any], handle: str,
                          actor: Actor) -> Mapping[str, Any]:
    """``view`` of a mail attachment (or a page or member of one) with the text
    ``plx.read`` serves this reader for it (``row.content_text``, through the runtime's
    one mail resolver and its gates, ``CaseRuntime.mail_capture`` with ``text``), so a
    passage a claim quotes from the attachment is checked against what the reader was
    shown; a page's view also carries that page's text as ``text``. A message, a view
    already holding the text, or a row the gate does not serve: ``view`` unchanged."""
    from integrations.captured_text import split_member
    from integrations.graph_evidence import is_mail_attachment_citation
    from ontology.mentions import cited_page
    row = view.get("row") if isinstance(view.get("row"), Mapping) else None
    if row is None or isinstance(row.get("content_text"), str) or "#" not in handle:
        return view
    try:
        member = split_member(cited_page(handle)[0])
        if not is_mail_attachment_citation(handle.split("#page=", 1)[0] if member is None
                                           else member[0]):
            return view
        served = runtime.mail_capture(handle, actor, text=True)
    except Exception:  # noqa: BLE001 - not served: the view as the gate read it
        return view
    served_row = served.get("row") if isinstance(served, Mapping) else None
    served_row = served_row if isinstance(served_row, Mapping) else {}
    text = served_row.get("content_text")
    # A member's row as the capture reads it from the container's pinned bytes (its
    # name, bytes and MIME type: ``captured_text.member_view``), which the gate-only
    # view never opens the container for: the claim gate counts a member as its own
    # record only on that row (``claim_checks._member_shown``).
    shown = ({k: v for k, v in served_row.items() if k != "content_text" and k not in row}
             if member is not None else {})
    if not isinstance(text, str) and not shown:
        return view
    out = {**view, "row": {**row, **shown,
                           **({"content_text": text} if isinstance(text, str) else {})}}
    if isinstance(view.get("page"), int) and "text" not in view and isinstance(text, str):
        out["text"] = text
    return out


def gate_rows(rows: Callable[[str, Actor], Mapping[str, Any] | None] | None, actor: Actor,
              as_known_at: str | None = None, *, moved_on: bool = False
              ) -> Callable[[str], Mapping[str, Any] | None]:
    """``check_items``'s ``rows`` for one reader over a row reader (``citation_rows``): any
    refusal is no row, and a changed-since view (a superseded mirror or BC capture
    revision, a live record that moved) keeps no row unless ``as_known_at`` falls before
    its supersession (``_superseded_by``): that revision was current then, the answer's
    own, and its row is checked as usual. None ``as_known_at``: now. ``moved_on`` (the
    answer gate's re-read, now): a view that keeps no row carries it as ``revision``, read
    only by the claim gate's moved-on check (``claim_checks._moved_on``: the same cited
    value on the source's current revision passes there, citing it); a plan's own checks
    keep a superseded revision changed since."""
    def read(handle: str) -> Mapping[str, Any] | None:
        try:
            view = rows(handle, actor)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - unreadable for this reader (or no reader): no row
            return None
        if not isinstance(view, Mapping) or view.get("changed_since") in (None, False):
            return view
        if _superseded_by(view, as_known_at):
            # A revision the source moved past before the answer's moment: the
            # projection is behind its capture, so the value is not the answer's.
            # For the answer gate its row stays only as ``revision``, read by nothing but
            # the moved-on check (``claim_checks._moved_on``: the cited cell holds the
            # same value on the current revision, else refused); no other check sees it.
            stale = {"citation": handle, "status": "changed_since", "changed_since": True,
                     "current_citation": view.get("current_citation")}
            return {**stale, "revision": dict(view)} if moved_on else stale
        return {**view, "changed_since": False}
    return read


def default_unit_contracts() -> Any:
    """The built-in role and unit contracts every claim gate checks against by default."""
    from operating.source_units import SourceUnitContracts
    return SourceUnitContracts.builtin()


def read_scope(tools: Any) -> Callable[[str, str, Actor], bool]:
    """``scope`` over an ``OntologyTools``: whether its read policy (as set for the call)
    shows the actor every object ``link_type`` can attach to ``root_id``.

    Markings are proven per object attached to this root, not per store: each object the
    store holds on that link (read for no reader, shown to none) may carry only markings the
    reader holds. ``covers.withheld`` counts the attached objects the reader's markings
    withhold, so a caller names them and never drops them. Audiences are decided from the
    policy and the schema alone, never from rows the reader cannot see, so the answer says
    nothing about whether a withheld object exists: the reader must read the audience of
    every binding at the link's other end, ``api:<relation>`` exactly; a Business Central
    binding as the unit ``bc:<company>/<relation>`` of the root's own company with no BC unit
    of the policy denied to it (a root that is not a BC record: the whole ``bc`` family);
    another source only as a family the policy fully covers (``ReadPolicy.covers_family``)
    and without item ACLs."""
    def withheld(link_type: str, root_id: str, actor: Actor) -> int | None:
        """How many attached objects carry a marking the reader lacks (None: undecided)."""
        try:
            with tools.store.lock:
                found = tools.store.linked_markings(root_id, link_type)
        except Exception:  # noqa: BLE001 - an undecided scope proves nothing
            return None
        held = tools.policy.held_markings(actor)
        return sum(not marks <= held for marks in found.values())

    def company_unit_granted(root_id: str, relation: str, actor: Actor) -> bool | None:
        """Whether the reader may read ``bc:<company>/<relation>`` of the root's own company
        and the policy names no BC unit that is left out of its grant (an explicit denial
        anywhere in the family, as ``covers_family``); None for a root that is not a BC
        record. The family-wide grant ``bc:*`` is not required: a link attaches objects of
        the root's own company."""
        with tools.store.lock:
            row = tools.store.objects([root_id]).get(root_id)
        key = str(row["audience_key"]) if row is not None else ""
        if not (key.startswith("bc:") and "/" in key):
            return None
        policy = tools.policy
        return policy.may_read(actor, f"{key.partition('/')[0]}/{relation}") and all(
            principals & actor.principals for pattern, principals in policy.audiences.items()
            if pattern.startswith("bc:") and pattern != "bc:*")

    def covers(link_type: str, root_id: str, actor: Actor) -> bool:
        policy, schema = tools.policy, tools.schema
        link = schema.link_types.get(link_type)
        if link is None or withheld(link_type, root_id, actor) != 0:
            return False
        binding = _binding_of(root_id)
        others = [b["from_binding"] if b.get("to_binding") == binding else b["to_binding"]
                  for b in link.get("bindings") or ()
                  if binding in (b.get("from_binding"), b.get("to_binding"))]
        sources = {b["id"]: (b["source"], b["relation"]) for spec in schema.object_types.values()
                   for b in spec.get("bindings") or ()}
        prefixes = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
        for other in others:
            source, relation = sources.get(other, (None, None))
            prefix = prefixes.get(source)
            if prefix is None or prefix in _ITEM_ACL_AUDIENCES:
                return False
            unit = company_unit_granted(root_id, relation, actor) if prefix == "bc" else None
            if not (policy.may_read(actor, f"api:{relation}") if prefix == "api"
                    else unit if unit is not None
                    else policy.covers_family(actor, prefix)):
                return False
        return bool(others)
    covers.withheld = withheld  # type: ignore[attr-defined]
    return covers


def capture_claims(tools: Any) -> Callable[[str, str, Actor], bool | None]:
    """``captured`` over an ``OntologyTools``: whether ``store_meta.source_freshness`` holds a
    complete ``company=<id>`` claim on BC ``relation`` for the root's company (True), only
    claims that did not finish or a stale BC capture (False), or none, or a root that is not
    a BC record (None). Read as ``plx.process`` reads it (``_scope_claims`` /
    ``_claim_cause``, no anchored claim: a per-record read never proves a whole set). A claim
    is no grant: None unless the policy (as set for the call) lets ``actor`` read that unit
    (``bc:<company>/<relation>``) and every marking a record of it can carry, so a hidden
    record never becomes a confident absence. A record's version carries exactly its type's
    markings (``ontology/project.py``), so those are the markings of every object type bound
    to the relation; a relation no type binds proves nothing (None). Markings on properties
    hide values, never records: a plan reading one checks it itself."""
    from types import SimpleNamespace
    from .ontology_tools_process import ProcessTools
    from .ontology_tools_process_answer import _INCOMPLETE, _scope_claims

    def record_markings(relation: str) -> set[str] | None:
        bound = [spec for spec in tools.schema.object_types.values()
                 if any(b.get("source") == "bc" and b.get("relation") == relation
                        for b in spec.get("bindings") or ())]
        return {m for spec in bound for m in spec.get("markings") or ()} if bound else None

    def captured(relation: str, root_id: str, actor: Actor) -> bool | None:
        with tools.store.lock:
            rows = tools.store.objects([root_id])
            store_meta = tools._store_meta()
        row = rows.get(root_id)
        if row is None or not str(row["audience_key"]).startswith("bc:"):
            return None
        unit = f"{str(row['audience_key']).partition('/')[0]}/{relation}"
        marks = record_markings(relation)
        if marks is None or not (tools.policy.may_read(actor, unit)
                                 and marks <= tools.policy.held_markings(actor)):
            return None
        if any(f.get("stale") for f in tools._freshness(store_meta, ("bc",))):
            return False
        ctx = SimpleNamespace(claims=_scope_claims(store_meta), obj=rows.get)
        cause = ProcessTools._claim_cause(ctx, "bc", relation, None, [root_id])
        return True if cause is None else (False if cause == _INCOMPLETE else None)
    return captured


def relation_complete(tools: Any) -> Callable[[str, Actor, str | None], str | None]:
    """``complete`` over an ``OntologyTools``: when a Portal API binding's relation is
    proven wholly captured for ``actor``, the time its last full traversal finished, else
    None. Read as ``plx.process`` proves a carrier complete (``_carrier_problem``,
    ``_relation_complete``): the answer is about now (no ``as_known_at`` before the
    host's clock); ``store_meta.relation_completeness`` holds a complete, not
    stale traversal of the relation finished by the answer's time; the Portal capture is not stale; the
    build report is readable and reports no gap that can hide a record of the binding;
    the policy lets the actor read the relation (``api:<relation>``) at every store
    marking. Any other source (BC has per-company claims, ``capture_claims``; others carry
    item-level permissions) is None, so a hidden record never becomes a confident zero."""
    from .ontology_tools_identity import ToolError
    from .ontology_tools_policy import _to_ms
    from .ontology_tools_process_answer import _build_report, _relation_claims
    from .ontology_tools_process_graphs import _BINDING_GAPS
    from ontology import resolve

    def complete(binding_id: str, actor: Actor, as_known_at: str | None) -> str | None:
        try:
            at = tools._as_known_at({} if as_known_at is None else {"as_known_at": as_known_at})
        except ToolError:  # an unreadable time proves nothing
            return None
        if as_known_at is not None and at < _to_ms(tools.clock()):
            return None  # absence is proven only for an answer about now
        spec = resolve.binding_spec(tools.schema, binding_id)
        if spec is None or spec[1].get("source") != "portal_api":
            return None
        tid, binding = spec
        marks = set(tools.schema.object_types[tid].get("markings", ())) | set(store_ddl.MARKINGS)
        if not (tools.policy.may_read(actor, f"api:{binding['relation']}")
                and marks <= tools.policy.held_markings(actor)):
            return None
        with tools.store.lock:
            store_meta = tools._store_meta()
        report = _build_report(store_meta)
        if report is None or any(
                g["where"] == binding_id and g["code"] in (*_BINDING_GAPS,
                                                         "source_capture_incomplete")
                for g in report):
            return None
        if any(f.get("stale") for f in tools._freshness(store_meta, ("portal_api",))):
            return None
        finished = _relation_claims(store_meta).get(("portal_api", binding["relation"]))
        if finished is None or finished > at:
            return None
        return finished.isoformat().replace("+00:00", "Z")
    return complete
