"""The ontology tools of one case runtime and actor: store, read policy, live fallback.

``case_host_tools`` builds the ``OntologyTools`` a case host (``operating/mcp_server.py``)
and the in-process case opener (``operating/case_workspace_live.py``) serve for one actor:

* the store: a built projection (``--ontology-store``), or an empty projection of the
  compiled schema when none exists, so the host is never blind (a filter or listing
  ``plx.find`` on it answers ``complete: false`` with a ``projection_not_built`` gap);
* ``sources``: find-live's ``ontology.resolve.SourceResolver`` over the readers the
  runtime already holds (the source mirror, and Business Central when configured).
  ``plx.find`` falls back to it when the store has no visible match: the typed keys of
  the query are read live where the policy grants the actor, projected into a
  throw-away store by the one projector, and answered by the unchanged find logic
  (``OntologyTools._find_sources``; a live no-match is never ``absent_in_source``);
* the policy (``case_host_policy``): the same audiences the readers enforce. BC units a
  built store holds (a BC capture projected by ``ontology.project_bc.BcCaptures``) are
  decided by the live audience too, with or without a configured BC reader, so a BC
  projection is served on its own. Teams channel and chat units a built store holds
  (``ontology.project_graph.TeamsCaptures``) are granted by the live audience too, and each
  ``teams-channel://`` or ``teams-chat://`` row passes the item gate:
  ``CaseRuntime.resolve_citation`` against the host's Teams capture (the reader in the
  conversation's captured membership), the same read the claim gate makes. Brain
  knowledge units a built store holds (``brain:<namespaceKey>``,
  ``ontology.project_knowledge.KnowledgeCaptures``) are granted by the host's namespace
  authority (``integrations.knowledge_source.NamespaceAuthority``, re-read every call;
  personal namespaces never; no authority, or one unreadable or malformed: nobody).

A persistent host publishes each projection generation by renaming a new file over
the store path (``operating.host._publish``). ``CaseHostTools.request`` scopes one
request: entering it reopens the store when the path names another file than the one
open, so every request runs wholly on one generation and the next one on the newest
(its digest, freshness and stale labels are that generation's).

``plx.identity``, ``plx.documents`` and ``plx.read`` (``operating.ontology_tools_evidence``)
are reachable through ``CaseHostTools.call`` and ``CaseHostTools.register`` under the same
per-call policy; ``plx.read`` and the operator originals of ``plx.documents`` read through
the runtime's own audience-checked readers (``RuntimeCitations``).

``plx.object`` and ``plx.around``/``plx.timeline`` read the projection store only. An
object id that ``plx.find`` found live is not in it (its throw-away store is discarded
after the answer), so they answer ``object_not_found`` / nothing reached for it; the
candidate's ``matched.raw.origin.citation`` is the cited source row, readable with
``plx.source_citation`` and citable on a case.

``plx.object`` answers one ``object_not_found`` for an absent, withheld or broken object,
so the answer is no existence oracle. ``CaseHostTools.not_found_reason`` types that answer
for an operator's check (``scripts/object_workspace_probe.py``), never for a reader:
``withheld`` (the reader's grant, markings or item gate), ``no_current_version``,
``gate_error`` or ``absent`` (``NOT_FOUND_REASONS``).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import dataclasses
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .ontology_tools import (_ITEM_ACL_SCHEMES, TOOL_NAMES, OntologyTools, ReadPolicy,
                             StoreReader, _envelope_bytes, _error, default_schema,
                             item_gate_from_case_runtime, load_contracts)

# Why plx.object answers object_not_found (``CaseHostTools.not_found_reason``).
NOT_FOUND_REASONS = ("withheld", "no_current_version", "gate_error", "absent")


def _review_store_gap(result: Any, error: Any, contract: Mapping[str, Any] | None,
                      max_bytes: int) -> Any:
    """``result`` of a call that read the review store as no reviews because it could not
    be read (``error``: ``build_steps.ReviewStoreUnreadable``): its ``gaps`` lead with a
    ``review_store_unreadable`` gap (within the tool's ``maxItems`` and ``max_bytes``, the
    last gaps giving way) and a complete answer is incomplete for that reason, so neither
    reads as "nobody reviewed this". An error result, or an answer with neither field, is
    returned as it is."""
    if not isinstance(result, dict) or "error" in result:
        return result
    out = dict(result)
    completeness = out.get("completeness")
    if isinstance(completeness, dict) and completeness.get("complete") is True:
        out["completeness"] = dict(completeness, complete=False, reason=error.code)
    if isinstance(out.get("gaps"), list):
        caps = [shape.get("properties", {}).get("gaps", {}).get("maxItems")
                for shape in (contract or {}).get("output", {}).get("oneOf", ())]
        cap = min((c for c in caps if isinstance(c, int)), default=None)
        gap = {"state": "inaccessible", "reason": error.code, "subject": error.path.name,
               "detail": "the review store is there but could not be read: this answer "
                         "holds none of its reviews (accepted or rejected links, reviewed "
                         "aliases, value reviews)"}
        out["gaps"] = [gap, *out["gaps"]][:cap]
        while len(out["gaps"]) > 1 and _envelope_bytes(out) > max_bytes:
            out["gaps"] = out["gaps"][:-1]
    return out


def table_tool_names() -> frozenset[str]:
    """The tools ``tool_table`` serves (``CaseHostTools.table``): none may be registered
    over them (``CaseHostTools.register_tool``)."""
    from . import (meeting_agenda, ontology_query, ontology_tools_changes,
                   ontology_tools_investigate, ontology_tools_process, ontology_tools_traverse)
    return frozenset(TOOL_NAMES) | frozenset(ontology_tools_traverse.TOOL_NAMES) | frozenset(
        ontology_tools_process.TOOL_NAMES) | frozenset(ontology_tools_investigate.TOOL_NAMES) \
        | frozenset(meeting_agenda.TOOL_NAMES) | frozenset(ontology_tools_changes.TOOL_NAMES) \
        | frozenset(ontology_query.TOOL_NAMES)


class _LazyProcess:
    """``plx.process`` for a tool table, its graphs loaded on the first call: a broken
    graph is that call's error, never a failure of the table's other tools."""

    def __init__(self, tools: OntologyTools) -> None:
        self.base, self.contracts = tools, tools.contracts
        self._process: Any = None

    def call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        if self._process is None:
            from .ontology_tools_process import ProcessTools
            self._process = ProcessTools(self.base)
        return self._process.call(name, arguments, actor)


def tool_table(tools: OntologyTools, runtime: Any, *,
               schema: Any = None, follow_ups: Any = None,
               label_reserve: int = 0, documents: Any = None) -> dict[str, dict[str, Any]]:
    """The generic tools over ``tools`` as one host tool table (name -> spec with
    ``handler(arguments, actor)``): plx.schema/find/object, plx.around/timeline,
    plx.process and plx.investigate, which dispatches its plans through this same table
    (so a three_way_match plan gets plx.process's verdicts) and checks its claims
    against the runtime's per-reader citation reads (``CaseRuntime.resolve_citation``) and
    proves a linked set whole only within the reader's policy (``read_scope``).
    ``follow_ups``: plx.investigate's follow-up recorder
    (``ontology_tools_investigate.follow_up_recorder``; none: the option is refused).
    ``label_reserve``: the bytes the host's labels add to a result after plx.investigate
    answers (``InvestigateTools``), kept free by its pages. ``documents``: the host's
    ``plx.read`` ``(handle, actor) -> result`` its engines read message text through (none:
    they read none). Every handler reads under ``tools.policy`` as the caller set it for
    this call."""
    from . import (ontology_tools, ontology_tools_evidence, ontology_tools_investigate,
                   ontology_tools_process, ontology_tools_traverse)
    table: dict[str, dict[str, Any]] = {}
    ontology_tools.register(table, tools)
    traverse = ontology_tools_traverse.TraverseTools(tools)
    # The store facts every reader's plx.around / plx.timeline needs: loaded from beside
    # the store file (``facts_path``: a build keeps them before it serves a generation);
    # when they are not there, read in the background by worker processes while the
    # table serves, never on the request path (``TraverseTools.prime_lazily``).
    traverse.prime_lazily(cell_families=ontology_tools_evidence.STORE_DOCUMENT_FAMILIES)
    ontology_tools_traverse.register(table, traverse)
    ontology_tools_process.register(table, _LazyProcess(tools))
    ontology_tools_investigate.register(table, ontology_tools_investigate.InvestigateTools(
        table, contracts=tools.contracts, schema=schema if schema is not None else tools.schema,
        rows=ontology_tools_investigate.citation_rows(
            runtime, held_markings=lambda actor: tools.policy.held_markings(actor)),
        scope=ontology_tools_investigate.read_scope(tools),
        captured=ontology_tools_investigate.capture_claims(tools),
        complete=ontology_tools_investigate.relation_complete(tools), follow_ups=follow_ups,
        reviews=traverse.review,
        held_markings=lambda actor: tools.policy.held_markings(actor),
        landed_actuals=landed_actuals_of(runtime),
        people=people_directory_of(runtime),
        documents=documents,
        label_reserve=label_reserve))
    # plx.agenda: a meeting's agenda read through this same table (operating/meeting_agenda.py).
    from . import meeting_agenda
    meeting_agenda.register(table, schema if schema is not None else tools.schema)
    # plx.changes: what changed since a time, over this table's store and read policy.
    from . import ontology_tools_changes
    ontology_tools_changes.register(table, tools, prime=True)
    # plx.query: one composable read (resolve, traverse, filter, aggregate) over this table.
    from . import ontology_query
    ontology_query.register(table, tools)
    return table


# How long ``prime_unbound`` waits for store facts read in the background before it
# leaves them to the first call that needs them (which then waits for that same read).
UNBOUND_FACTS_WAIT_SECONDS = 1800.0


def prime_unbound(store_path: str | Path, *, tenant: str = "unbound",
                  wait: float = UNBOUND_FACTS_WAIT_SECONDS) -> dict[str, Any]:
    """Every reader-independent preparation a host's first call otherwise makes, done
    before the host knows its reader (a warm host, ``mcp_server._bind_warm_ticket``): the
    compiled schema; the served store's facts (``TraverseTools.prime``: its audiences,
    gated handles, event types and document cell spans) and audience keys; the row-delta
    overlay's layers (their owned ids, audience keys and which layer serves each id) and
    each layer's store facts. Facts kept beside a store file are loaded; a store file
    whose facts are not kept yet has them read now by worker processes and kept for
    every later process (one read per file across processes), and this waits for that
    read at most ``wait`` seconds. Everything is kept per process by file identity, so the
    session the host serves next finds it; nothing here names, grants or reads for a
    reader (the policy grants nothing; no live source is asked). Never raises: what
    failed is read by the call that needs it, as before. Returns what was primed
    (counts and states only)."""
    from . import delta_overlay
    from .ontology_tools_evidence import STORE_DOCUMENT_FAMILIES
    from .ontology_tools_traverse import TraverseTools, kept_audience_keys
    from .pg import ontology_reader

    out: dict[str, Any] = {"store": "absent", "layers": 0, "layers_kept": 0}
    if store_path is None or not Path(store_path).is_file():
        return out
    families = STORE_DOCUMENT_FAMILIES
    reader = overlay = None
    try:
        schema = default_schema()
        if ontology_reader.serves(store_path):
            out["store"] = "postgres"   # its reader holds its facts itself: per process
            return out
        reader = StoreReader.open(store_path)
        tools = OntologyTools(reader, ReadPolicy(tenant, {}), schema=schema)
        kept_audience_keys(reader)
        traversals = [TraverseTools(tools)]
        traversals[0].prime_lazily(cell_families=families)
        pointer = delta_overlay.pointer_path(store_path)
        stamp = delta_overlay.pointer_stamp(pointer)
        overlay = None if stamp is None else delta_overlay.open_overlay(pointer, stamp, tools)
        for layer in overlay.layers if overlay is not None else ():
            traversals.append(layer.traverse)   # primes it (kept, or read in the background)
        out["layers"] = len(traversals) - 1
        end = time.monotonic() + wait
        loaded = []
        for traverse in traversals:
            pending = getattr(traverse, "_pending", None)
            if pending is not None:
                pending[2].wait(max(0.0, end - time.monotonic()))
            # the facts into this process's kept facts (``_load_facts``): True once kept
            loaded.append(traverse.prime_lazily(cell_families=families))
        out["store"] = "kept" if loaded[0] else "unprimed"
        out["layers_kept"] = sum(loaded[1:])
    except Exception as error:  # noqa: BLE001 - a warm-up never fails a host
        out["error"] = type(error).__name__
    finally:
        if overlay is not None:
            overlay.close()
        if reader is not None:
            try:
                reader.conn.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def people_directory_of(runtime: Any) -> Any:
    """The people directory provider of a runtime (``operating.people_graph.directory_of``:
    its people guard and mail capture, read anew per call), or None for a runtime with no
    state directory: the people engines then name nobody (fail closed)."""
    if not isinstance(getattr(runtime, "state_dir", None), (str, Path)):
        return None
    from .people_graph import directory_of
    return directory_of(runtime)


def landed_actuals_of(runtime: Any) -> Any:
    """The recorded duty and freight charges captured on this host
    (``<state_dir>/landed-actuals``, ``duty_actual.ActualsFile``), or None for a runtime with no
    state directory. A host without the capture answers LANDED_ACTUALS_NOT_CAPTURED, a named
    gap, never 'no charge'."""
    state_dir = getattr(runtime, "state_dir", None)
    if not isinstance(state_dir, (str, Path)):
        return None
    from .duty_actual import ActualsFile
    return ActualsFile.under(state_dir)


def live_lookups(runtime: Any, *, bc: bool = True,
                 bc_books: Mapping[str, str] | None = None,
                 max_staleness: float | None = None,
                 captured_bound: float | None = None,
                 freshness_clock: Callable[[], Any] | None = None) -> dict[str, Any]:
    """``SourceLookup`` per source family over the runtime's own readers (GET only);
    ``max_staleness`` bounds the age of the mirror pages the Portal lookup answers from;
    ``captured_bound``: the mirror is the host's ingest capture, never refreshed here,
    stale past that age (``MirrorLookup``)."""
    from ontology.resolve import BcLookup, MirrorLookup
    lookups: dict[str, Any] = {"portal_api": MirrorLookup(
        runtime.mirror, max_staleness=max_staleness, captured_bound=captured_bound,
        freshness_clock=freshness_clock)}
    live = getattr(runtime, "live", None)
    if bc and live is not None and live.bc is not None:
        lookups["bc"] = BcLookup(live.bc, actor_for=runtime.employee_gate, books=bc_books)
    return lookups


# The versions whose handle starts ``bc://`` in any letter case (what ``LIKE 'bc://%'``
# matches), as index ranges: ``[p, p[:-1] + '0')`` holds exactly the strings starting with
# ``p`` (``0`` follows ``/``). A ``LIKE`` read every version's handle in the index
# (11.6 million on generation 7, ~40 s at every host start); the ranges read the BC ones.
_BC_HANDLE_RANGES = " OR ".join(
    f"(v.l1_handle >= '{p}' AND v.l1_handle < '{p[:-1]}0')"
    for p in ("bc://", "bC://", "Bc://", "BC://"))


def store_bc_units(reader: StoreReader) -> dict[str, str]:
    """BC capture units a store holds (``bc:<company>/<entitySet>``) -> the BC
    environment their capture handles name (``bc://<environment>/...``)."""
    units: dict[str, str] = {}
    with reader.lock:
        rows = reader.conn.execute(
            "SELECT o.audience_key, MIN(v.l1_handle) FROM object_versions v JOIN objects o "
            "ON o.object_id = v.object_id WHERE o.audience_key LIKE 'bc:%' "
            f"AND ({_BC_HANDLE_RANGES}) GROUP BY o.audience_key").fetchall()
    for key, handle in rows:
        units[key] = handle[len("bc://"):].split("/", 1)[0]
    return units


_BC_UNITS: dict[tuple[int, int], dict[str, str]] = {}
_BC_UNITS_LOCK = threading.Lock()


def served_bc_units(reader: StoreReader, identity: tuple[int, int] | None) -> dict[str, str]:
    """``store_bc_units`` of a served generation, read once per process per generation
    file (``identity``: its device and inode; a generation is never written again);
    None: read every time."""
    if identity is None:
        return store_bc_units(reader)
    with _BC_UNITS_LOCK:
        held = _BC_UNITS.get(identity)
    if held is None:
        held = store_bc_units(reader)
        with _BC_UNITS_LOCK:
            _BC_UNITS.clear()   # one served generation at a time
            _BC_UNITS[identity] = held
    return dict(held)


def case_host_policy(runtime: Any, actor: Any, schema: Any, *,
                     audience_keys: Iterable[str] = (),
                     bc_lookup: Any = None,
                     bc_units: Mapping[str, str] | None = None,
                     knowledge_namespaces: str | Path | None = None,
                     marking_grants: Mapping[str, frozenset[str]] | None = None
                     ) -> ReadPolicy:
    """The actor's ``ReadPolicy`` from the audiences the readers enforce, decided now
    and default deny: ``api:<relation>`` for every mirror relation the schema binds or
    the store holds (granted when the source-mirror audience lets the actor read it,
    principals and clearance), every BC company/entity-set unit of a schema
    binding (granted when the live audience allows the employee-gated actor; one BC
    companies read), and every BC unit the store holds (``bc_units``: unit ->
    environment, ``store_bc_units``; granted when the live audience allows the
    employee-gated actor every resource of the unit, a line set's header set too, as the
    capture store decides), and every Mission Control audience key the store holds
    (``mc:<scope>/<collection>[/<project>]``, granted when the live audience allows the
    employee-gated actor every resource of it, ``integrations.mc_sources.
    audience_resources``: ``mc:<scope>:<collection>`` and, for a collection MC scopes per
    project, ``mc:<scope>:project:<project>``), and every Teams channel or chat unit the
    store holds (``teams:channel:<team>/<channel>``, ``teams:chat:<chat>``, granted when the
    live audience allows the employee-gated actor the conversation,
    ``teams:channel:<team>:<channel>`` / ``teams:chat:<chat>``; membership is the item
    gate's, per row), and every SharePoint list unit the store holds
    (``sharepoint-list:<site>/<list>``, granted when the live audience allows the
    employee-gated actor the list, ``sharepoint:list:<site>:<list>``; item permissions are
    the item gate's, per row), and every brain knowledge unit the store holds
    (``brain:<namespaceKey>``, granted when the ``plx-knowledge-namespaces/v1`` document at
    ``knowledge_namespaces``, read now, lets the employee-gated actor read the namespace:
    a personal or unknown namespace never, nor any when the document is absent, unreadable
    or malformed). Exact keys only; the BC units are also listed (``ReadPolicy.units``), so
    a reader granted every BC unit the store holds covers BC. A marking is
    held only by the principals the host config grants it (``marking_grants``, checked by
    ``check_marking_grants``); none granted: held by nobody."""
    principals = frozenset(actor.principals)
    audience = runtime.mirror._audience()
    grants: dict[str, frozenset[str]] = {}
    relations = set()
    if schema is not None:
        relations = {(b["source"], b["relation"]) for spec in schema.object_types.values()
                     for b in spec.get("bindings", ())}
    mirrored = {relation for source, relation in relations if source == "portal_api"}
    mirrored |= {str(key).partition(":")[2] for key in audience_keys
                 if str(key).startswith("api:")}
    for relation in sorted(mirrored - {""}):
        grants[f"api:{relation}"] = (principals if audience.allows(actor, relation)
                                     else frozenset())
    if bc_lookup is not None:
        from integrations.capture_store import bc_unit
        state, companies, _ = bc_lookup.companies(actor)
        gated = runtime.employee_gate(actor)
        live_audience = runtime.live.audience
        for company in companies if state == "read" else ():
            for source, relation in sorted(relations):
                if source != "bc":
                    continue
                try:
                    resource = bc_lookup.reader.resource(company, relation)
                except ValueError:
                    continue  # an entity set outside the reader's allowlist
                grants[bc_unit(company, relation)] = (
                    principals if live_audience.allows(gated, resource) else frozenset())
    live = getattr(runtime, "live", None)
    if bc_units and live is not None:
        from integrations.capture_store import BC_KIND
        gated = runtime.employee_gate(actor)
        for unit, environment in sorted(bc_units.items()):
            if unit in grants:
                continue
            company, _, entity_set = unit.partition(":")[2].partition("/")
            try:
                path = BC_KIND.canonical_path((environment, company, entity_set, company))
            except ValueError:
                continue  # not a capture unit: never granted
            grants[unit] = (principals if all(live.audience.allows(gated, resource)
                                              for resource in BC_KIND.resources(path))
                            else frozenset())
    mc_units = sorted(str(k) for k in audience_keys if str(k).startswith("mc:"))
    if mc_units and live is not None:
        from integrations.mc_sources import audience_resources
        gated = runtime.employee_gate(actor)
        for unit in mc_units:
            try:
                resources = audience_resources(unit)
            except ValueError:
                continue  # not an MC audience key (or one without its project): never granted
            grants[unit] = (principals if all(live.audience.allows(gated, resource)
                                              for resource in resources) else frozenset())
    teams_units = sorted(str(k) for k in audience_keys if str(k).startswith(_TEAMS_UNITS))
    if teams_units and live is not None:
        from integrations.capture_teams import CHANNEL_KIND, CHAT_KIND
        gated = runtime.employee_gate(actor)
        for unit in teams_units:
            kind = CHANNEL_KIND if unit.startswith(_TEAMS_UNITS[0]) else CHAT_KIND
            # The conversation's parts and a placeholder message id; a unit that does not
            # name exactly one conversation of its kind is never granted.
            parts = unit.partition(":")[2].partition(":")[2].split("/")
            try:
                path = kind.canonical_path((*parts, "0"))
            except ValueError:
                continue
            allowed = kind.unit(path) == unit and all(
                live.audience.allows(gated, resource) for resource in kind.resources(path))
            grants[unit] = principals if allowed else frozenset()
    list_units = sorted(str(k) for k in audience_keys if str(k).startswith(_SHAREPOINT_LIST))
    if list_units and live is not None:
        from integrations.capture_sharepoint_lists import LIST_KIND
        gated = runtime.employee_gate(actor)
        for unit in list_units:
            site, sep, list_id = unit[len(_SHAREPOINT_LIST):].partition("/")
            try:
                path = LIST_KIND.canonical_path((site, list_id, "1"))
            except ValueError:
                continue  # not a list unit: never granted
            allowed = sep and LIST_KIND.unit(path) == unit and all(
                live.audience.allows(gated, resource) for resource in LIST_KIND.resources(path))
            grants[unit] = principals if allowed else frozenset()
    brain_units = sorted(str(k) for k in audience_keys if str(k).startswith(_BRAIN))
    if brain_units and knowledge_namespaces is not None:
        from integrations.knowledge_source import NamespaceAuthority
        try:
            authority = NamespaceAuthority.from_file(knowledge_namespaces)
        except (OSError, ValueError):
            authority = None  # unreadable or malformed: no namespace is granted
        if authority is not None:
            gated = runtime.employee_gate(actor)
            for unit in brain_units:
                grants[unit] = principals if authority.allows(gated, unit) else frozenset()
    # A declared full-read grant with its ``finance`` scope holds the finance and price
    # markings (``employee_permissions.full_read_marking_grants``); anyone else: the host's.
    from .employee_permissions import full_read_marking_grants
    gate = getattr(runtime, "employee_gate", None)
    try:
        entitlement = gate.entitlement(actor) if gate is not None else None
    except Exception:  # noqa: BLE001 - a refused or unresolved employee holds no more
        entitlement = None
    # Calendar events, meetings, Planner tasks and transcripts (``integrations.
    # capture_meetings``): every ``meetings:`` unit the store holds is decided here, never
    # left to default deny. A unit is granted when the live audience allows the
    # employee-gated actor every resource of it (a calendar or an organizer's transcripts:
    # ``mail:<oid>``, a plan: ``teams:planner:<plan>``); a calendar or an organizer's unit
    # (a person's mailbox) also needs an owner the people exclusion clears (fail closed: a
    # missing or unavailable guard clears nobody) and to be the reader's own mailbox (its
    # object id the employee's) or a full-read grant over every mailbox (``mail`` scope,
    # ``mail_scope`` ``all``). The item gate still decides each record by its captured ACL
    # (mailbox owner, plan people, participants), on every route (the tools and search:
    # ``ontology_tools._ITEM_ACL_SCHEMES``). A unit whose id is not of its kind's grammar
    # (an object id, a Planner id) is never granted: a key such as ``meetings:plan:*``
    # never acts as a pattern. "Own" is the employee's principal object id only, the key a
    # calendar is captured under; a calendar captured under another id of theirs is not.
    meetings_units = sorted(str(k) for k in audience_keys if str(k).startswith(_MEETINGS))
    if meetings_units and live is not None:
        from integrations.capture_meetings import EVENT_KIND, TASK_KIND, TRANSCRIPT_KIND
        from integrations.people_exclusion import is_object_id
        from .ontology_tools_evidence import people_excluded
        kinds = {"calendar": EVENT_KIND, "plan": TASK_KIND, "organizer": TRANSCRIPT_KIND}
        gated = runtime.employee_gate(actor)
        guard = getattr(runtime, "people_exclusion", None)
        own: set[str] = set()
        every_mailbox = False
        if entitlement is not None and entitlement.actor is not None:
            own = {str(entitlement.principal.object_id).strip().lower()}
            try:
                every_mailbox = entitlement.full_read_covers("mail") \
                    and entitlement.full_read.grant.mail_scope == "all"
            except Exception:  # noqa: BLE001 - an undecidable grant opens no mailbox
                every_mailbox = False
        for unit in meetings_units:
            part, _, owner = unit[len(_MEETINGS):].partition(":")
            kind = kinds.get(part)
            if kind is None or not owner or kind.unit((owner,)) != unit:
                continue  # not a meetings unit: never granted
            try:
                # the unit's id in its kind's grammar (a placeholder record id beside it)
                kind.canonical_path((owner, owner) if kind is TASK_KIND
                                    else (owner, "0", "0")[:kind.path_parts])
            except (ValueError, TypeError):
                continue  # an id outside the grammar (``*``, spaces): never granted
            try:
                allowed = all(live.audience.allows(gated, resource)
                              for resource in kind.resources((owner,)))
            except Exception:  # noqa: BLE001 - an undecidable audience grants nothing
                allowed = False
            if allowed and kind is not TASK_KIND:
                allowed = (is_object_id(owner) and not people_excluded(guard, [owner])
                           and (owner.lower() in own or every_mailbox))
            grants[unit] = principals if allowed else frozenset()
    # BC is decided unit by unit as mail and SharePoint are: every BC unit the store holds
    # (and every one decided above) is listed, so a reader granted all of them reads the
    # whole family (``covers_family``: a BC record reached through a Portal twin is then
    # proven visible) and one lacking any, a held unit left undecided included, does not.
    units: dict[str, tuple[str, ...]] = {}
    if bc_units:
        units["bc"] = tuple(sorted(set(bc_units) | {k for k in grants if k.startswith("bc:")}))
    return ReadPolicy(actor.tenant, grants,
                      full_read_marking_grants(marking_grants, entitlement), units,
                      unrestricted=entitlement is not None and entitlement.unrestricted())


# A granted principal: ``<kind>:<name>``, both parts non-empty, no whitespace.
_PRINCIPAL = re.compile(r"[A-Za-z0-9_.-]+:\S+")


def check_marking_grants(value: Any) -> dict[str, frozenset[str]]:
    """The host config's ``marking_grants`` (marking -> the principals who hold it), or
    ValueError: a marking the store does not know, a principal that is not a
    ``<kind>:<name>`` string with both parts non-empty and no whitespace, a wildcard, or
    anything not shaped so. Absent (None):
    nobody holds any marking."""
    from ontology.store_ddl import MARKINGS
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("marking_grants_invalid")
    out: dict[str, frozenset[str]] = {}
    for marking, principals in value.items():
        if marking not in MARKINGS or not isinstance(principals, list) or any(
                not isinstance(p, str) or "*" in p or not _PRINCIPAL.fullmatch(p)
                for p in principals):
            raise ValueError("marking_grants_invalid")
        out[marking] = frozenset(principals)
    return out


# integrations.capture_teams.CHANNEL_KIND and CHAT_KIND units
_TEAMS_UNITS = ("teams:channel:", "teams:chat:")
_SHAREPOINT_LIST = "sharepoint-list:"  # integrations.capture_sharepoint_lists.LIST_KIND units
_BRAIN = "brain:"  # integrations.knowledge_source.BRAIN_KIND units
_MEETINGS = "meetings:"  # integrations.capture_meetings units (calendar, plan, organizer)


def _file_identity(path: Path) -> tuple[int, int] | None:
    """The file ``path`` names now, or None. A generation is published by renaming a
    new file over the path and never written again, so the inode tells them apart."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_dev, found.st_ino)


def _file_stamp(path: Any) -> tuple | None:
    """A file's state now (device, inode, change time, size) with its write-ahead log's,
    or None for no path; a missing file is ``-1``. Any commit or rename changes it."""
    if path is None:
        return None
    out: list[Any] = []
    for name in (Path(path), Path(f"{path}-wal")):
        try:
            found = os.stat(name)
            out.append((found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size))
        except OSError:
            out.append(-1)
    return tuple(out)


# The longest one answer's reads keep the policy decided at its start (``policy_pinned``).
POLICY_PIN_SECONDS = 120.0
# How often a host asks whether Postgres holds the served generation (``_store_moved``).
PG_CHECK_SECONDS = 30.0


# The delta overlay's layers are read side by side (``_each_layer``): a find reads every
# layer, up to ``delta.MAX_LAYERS``, and one after another they cost seconds per find.
LAYER_WORKERS = max(2, min(16, os.cpu_count() or 2))
_LAYER_POOL: Any = None
_LAYER_POOL_LOCK = threading.Lock()


def _layer_pool() -> Any:
    global _LAYER_POOL
    with _LAYER_POOL_LOCK:
        if _LAYER_POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _LAYER_POOL = ThreadPoolExecutor(LAYER_WORKERS, thread_name_prefix="plx-layer")
        return _LAYER_POOL


def _each_layer(layers: Sequence[Any], read: Callable[[Any], Any]) -> list[Any]:
    """``read(layer)`` of every layer, in the layers' order, read side by side: the
    calling thread reads too, taking the next unread layer, so a busy (or nested) pool
    never holds it back; each read runs in a copy of the caller's context (its deadline,
    its run's reader decisions). Any read's exception is raised once every read ended."""
    import contextvars
    count = len(layers)
    if count < 2:
        return [read(layer) for layer in layers]
    out: list[Any] = [None] * count
    failed: list[BaseException] = []
    taken = iter(range(count))
    taking = threading.Lock()
    context = contextvars.copy_context()

    def drain() -> None:
        while True:
            with taking:
                index = next(taken, None)
            if index is None:
                return
            try:
                out[index] = context.copy().run(read, layers[index])
            except BaseException as error:  # noqa: BLE001 - raised to the caller below
                failed.append(error)

    pool = _layer_pool()
    helpers = [pool.submit(drain) for _ in range(min(count, LAYER_WORKERS) - 1)]
    drain()
    for helper in helpers:
        if not helper.cancel():   # one never started has nothing left to read
            helper.result()
    if failed:
        raise failed[0]
    return out


class CaseHostTools:
    """``OntologyTools`` for one runtime with the live fallback, plus the temporary
    directory holding the empty store and the resolver's throw-away stores. ``tools`` is
    built on first use; ``call`` decides the read policy again for every call (the
    audiences are re-read; a successful BC company listing is reused, a failed one is
    read again next call), so a revocation applies at once."""

    def __init__(self, runtime: Any, actor: Any, *, store_path: str | Path | None = None,
                 schema: Any = None, grammar: Any = None, bc: bool = True,
                 bc_books: Mapping[str, str] | None = None,
                 exclude_source: str | None = None, reserve_bytes: int = 0,
                 mail_capture_dir: str | Path | None = None,
                 max_staleness: float | None = None, reviews: Any = None,
                 portal_captured: bool = False, follow_ups: Any = None,
                 knowledge_namespaces: str | Path | None = None,
                 marking_grants: Mapping[str, Iterable[str]] | None = None,
                 freshness_bounds: Mapping[str, float] | None = None,
                 freshness_clock: Callable[[], Any] | None = None) -> None:
        self.runtime, self.actor = runtime, actor
        # The "now" a capture's age is judged at against ``freshness_bounds`` (None: the
        # wall clock); never the tools' own clock (``as_known_at``, record times).
        self.freshness_clock = freshness_clock
        # Source family -> seconds: how old this host's own capture of it may be before
        # it is stale (``operating.host.freshness_bounds``, from the configured ingest
        # cadence); none: the projection states no capture stale by its age.
        self.freshness_bounds = dict(freshness_bounds or {})
        # The host config's marking grants (``case_host_policy``); none: no marking is held.
        self.marking_grants = check_marking_grants(None if marking_grants is None else {
            marking: list(principals) for marking, principals in marking_grants.items()})
        # The host's brain namespace authority file (``case_host_policy``); none: no
        # brain unit is granted.
        self.knowledge_namespaces = knowledge_namespaces
        # plx.investigate's follow-up recorder (``tool_table``); none: the option is refused.
        self.follow_ups = follow_ups
        # The host serving this state directory captures the Portal into its mirror
        # (``operating.host``): configured, though this process reads no Portal itself.
        self.portal_captured = portal_captured
        # ontology.reviews.ReviewStore: plx.find reads its reviewed aliases and
        # plx.review (operating.ontology_tools_review.register) writes it.
        self.reviews = reviews
        self.max_staleness = max_staleness  # of the mirror pages a live find reads
        self.store_path = None if store_path is None else Path(store_path)
        self._schema, self._grammar = schema, grammar
        self.bc, self.bc_books, self.exclude_source = bc, bc_books, exclude_source
        # Room a host keeps beside each result (its _meta): results are bounded to
        # limits.max_result_bytes minus this.
        self.reserve_bytes = reserve_bytes
        # Room this host's labels take beside a plx.investigate page (``tool_table``).
        self.label_reserve = 0
        self._temp: tempfile.TemporaryDirectory | None = None
        self._tools: OntologyTools | None = None
        self._reader_keys: list[str] = []
        # The served store's own audience keys (``_reader_keys`` adds the overlay's).
        self._base_reader_keys: list[str] = []
        # The row-delta overlay over the served generation (``operating/delta_overlay``):
        # the opened one, the pointer state it was decided at (opened, absent or refused),
        # and whether this request checked the pointer.
        self._overlay: Any = None
        self._overlay_stamp: Any = None
        self._overlay_checked = False
        self._bc_units: dict[str, str] = {}
        self._bc_lookup: Any = None
        self._reader: StoreReader | None = None
        self._store_identity: tuple[int, int] | None = None  # the file the reader opened
        self._depth = 0  # requests in progress (``request``)
        self._pg_checked = 0.0   # when ``_store_moved`` last asked Postgres
        # One answer's policy (``policy_pinned``): (actor, policy, until), else None.
        self._pin: tuple[Any, ReadPolicy, float] | None = None
        self._answers: Any = None  # plx.aggregate / plx.compare, built on first use
        # The versioned mail capture plx.read reads first (else the live reader): the
        # runtime's (``CaseRuntime.mail_capture_dir``), the one the claim gate and a case's
        # digest read, so every reader of a graph-mail:// version reads the same row. A
        # host naming another one is refused rather than served two views of one handle.
        held = getattr(runtime, "mail_capture_dir", None)
        if not isinstance(held, (str, Path)):
            held = None
        if (mail_capture_dir is not None and held is not None
                and Path(mail_capture_dir).resolve() != Path(held).resolve()):
            raise ValueError("mail_capture_dir_not_the_runtime_capture")
        self.mail_capture_dir = held if mail_capture_dir is None else mail_capture_dir
        self._evidence: Any = None
        # Tools served through ``call`` besides the ontology tools (``register_tool``).
        self._registered: dict[str, Any] = {}
        self._table: dict[str, dict[str, Any]] | None = None

    def close(self) -> None:
        """Release the store connection and remove the working directory (the empty
        store and the resolver's throw-away stores). Called when the session ends and
        when a request finds a newer projection generation; the next call builds them
        again."""
        from . import ontology_tools_changes
        ontology_tools_changes.close_tools(self._table)   # its kept index's read connection
        if self._reader is not None:
            closer = getattr(self._reader, "close", None)
            closer() if closer is not None else self._reader.conn.close()
        self._drop_overlay()
        if self._temp is not None:
            self._temp.cleanup()
        (self._temp, self._tools, self._reader, self._answers, self._evidence,
         self._table) = (None, None, None, None, None, None)

    @contextmanager
    def request(self) -> Iterator["CaseHostTools"]:
        """One request (a tool call, or a console answer of several calls). Entering the
        outermost one drops what was built over an older projection generation, so the
        request reads the newest one; nested ones and the rest of the request keep it.
        The newer generation is opened here, before the call runs (``open_generation``),
        so the call does not pay the store-wide reads. A missing store file keeps the
        generation open. Requests are not concurrent (one connection, one serving
        thread). The mail reader's directory lookups (a mailbox's identity set, never
        mail content) are shared for the request (``GraphMailReader.directory_cache``):
        an item gate that re-reads many messages of one mailbox asks the directory once,
        not once per message; every audience, owner ACL and message decision is still
        made per item."""
        swapped = False
        if self._depth == 0 and self._reader is not None and self.store_path is not None:
            now = _file_identity(self.store_path)
            if now is not None and (now != self._store_identity or self._store_moved()):
                self.close()
                # The case runtime's capture gates read the projection too: free the
                # older generation's file now, not at their next resolve.
                close_captures = getattr(getattr(self.runtime, "captures", None), "close",
                                         None)
                if callable(close_captures):
                    close_captures()
                swapped = True
        mail = getattr(getattr(self.runtime, "live", None), "mail", None)
        block = getattr(mail, "directory_cache", None)
        if self._depth == 0:
            self._overlay_checked = False   # the pointer is read again by this request
        self._depth += 1
        try:
            with block() if callable(block) else nullcontext():
                if swapped:
                    self._open_table()
                yield self
        finally:
            self._depth -= 1

    def _store_moved(self) -> bool:
        """Whether the store this host reads should change between SQLite and Postgres
        (``PLX_ONTOLOGY_PG``: Postgres took the served generation in, by a load or a swap,
        or no longer holds it): checked at most every ``PG_CHECK_SECONDS``."""
        from operating.pg import ontology_reader
        if not ontology_reader.enabled() and not hasattr(self._reader, "text_reader"):
            return False
        now = time.monotonic()
        if now - self._pg_checked < PG_CHECK_SECONDS:
            return False
        self._pg_checked = now
        on_pg = hasattr(self._reader, "text_reader")
        return ontology_reader.serves(self.store_path) != on_pg

    def check_overlay(self) -> None:
        """Open the row-delta overlay this request reads (its pointer read again), before
        anything is decided for the request: the reader's policy for the whole request is
        then decided over the overlay's audience keys too, as each tool call's is."""
        self.tools  # noqa: B018 - built on first use
        self._check_overlay()

    def overlay_stamp(self) -> Any:
        """The row-delta pointer's state this host last read its overlay at (each delta
        cycle a new one; None: no overlay). Reading Postgres (``PLX_ONTOLOGY_PG``), which
        takes every layer in place: its data version, new with each applied layer."""
        reader = self._reader
        if reader is not None and hasattr(reader, "text_reader"):
            try:
                with reader.lock:
                    return ("postgres",
                            reader.conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.Error:
                return None
        return self._overlay_stamp

    def delta_layer_stamp(self) -> Any:
        """The delta layers the pointer names now, as plain values (their store paths,
        newest first; the same in every process): a page kept under it is new with each
        delta cycle. None: no pointer (or no store). Read from the pointer file itself,
        not this host's last overlay read, so a page never outlives a layer."""
        if self.store_path is None:
            return None
        try:
            from . import delta_overlay
            pointer = delta_overlay.read_pointer(delta_overlay.pointer_path(self.store_path))
        except Exception:  # noqa: BLE001 - an unreadable pointer keys nothing kept
            return ("unreadable",)
        return None if pointer is None else tuple(pointer["layers"])

    def store_meta(self) -> dict[str, str]:
        """``store_meta`` of the generation this host reads (its digest, freshness)."""
        reader = self.tools.store
        with reader.lock:
            return reader.meta()

    @property
    def schema(self) -> Any:
        if self._schema is None:
            self._schema = default_schema()
        return self._schema

    @property
    def tools(self) -> OntologyTools:
        if self._tools is None:
            from ontology import identifiers
            from ontology.project import build_store
            from ontology.resolve import ReviewedAliases, SourceResolver
            self._temp = tempfile.TemporaryDirectory(prefix="plx-case-find-")
            work = Path(self._temp.name)
            store = self.store_path
            if store is None:
                store = work / "projection.sqlite"
                build_store(store, self.schema, {}, normalizer=identifiers.normalize)
            else:   # before opening: a swap in between is seen by the next request
                self._store_identity = _file_identity(store)
            from operating.pg import ontology_reader
            if self.store_path is not None and ontology_reader.serves(store):
                # PLX_ONTOLOGY_PG: the Postgres ontology, current to the last delta
                # layer (no overlay); the served generation keeps the word index
                reader = self._reader = ontology_reader.PgStoreReader.open(text_store=store)
            else:
                reader = self._reader = StoreReader.open(store)
            # a call's time limit interrupts a store statement still running at it
            from .call_limits import interruptible
            interruptible(reader.conn)
            # A host whose Portal its own ingest captures and that holds no Portal client
            # serves the mirror as that capture, judged by the capture's bound, never
            # refreshed by this reader (``MirrorLookup(captured_bound=)``).
            captured = (self.freshness_bounds.get("portal_api") if self.portal_captured
                        and not getattr(self.runtime, "client_configured", True) else None)
            lookups = {source: lookup for source, lookup in
                       live_lookups(self.runtime, bc=self.bc, bc_books=self.bc_books,
                                    max_staleness=self.max_staleness,
                                    captured_bound=captured,
                                    freshness_clock=self.freshness_clock).items()
                       if source != self.exclude_source}
            from .ontology_tools_traverse import kept_audience_keys
            kept_audience_keys(reader)   # the generation's kept facts: no object scan
            self._reader_keys = reader.audience_keys()
            self._base_reader_keys = list(self._reader_keys)
            self._bc_units = served_bc_units(reader, self._store_identity)
            self._bc_lookup = lookups.get("bc")
            policy = ReadPolicy(self.actor.tenant, {})  # decided again on every call
            resolver = SourceResolver(self.schema, lookups, normalizer=identifiers.normalize,
                                      workdir=work / "live")
            contracts = load_contracts()
            contracts["limits"] = dict(contracts["limits"], max_result_bytes=(
                contracts["limits"]["max_result_bytes"] - self.reserve_bytes))
            self._tools = OntologyTools(
                reader, policy, schema=self.schema, contracts=contracts,
                grammar=self._grammar if self._grammar is not None else identifiers,
                sources=resolver, item_gate=item_gate_from_case_runtime(self.runtime),
                aliases=None if self.reviews is None else ReviewedAliases(self.reviews),
                reviews=self.reviews,
                # No Workbench API client and no host capture of the Portal: the Portal
                # is not configured on this host.
                unconfigured=() if self.portal_captured
                or getattr(self.runtime, "client_configured", True) else ("portal_api",),
                freshness_bounds=self.freshness_bounds,
                freshness_clock=self.freshness_clock)
            # The empty placeholder store is not a projection: filter/listing finds
            # on it report projection_not_built and complete=false.
            self._tools.projection_built = self.store_path is not None
        return self._tools

    def policy_now(self, actor: Any) -> ReadPolicy:
        """The policy for ``actor`` (already through the employee gate) decided now from
        the readers' audiences: a revoked grant applies to the next call."""
        return self._decide_policy(actor, self._bc_lookup)

    def stored_policy(self, actor: Any) -> ReadPolicy:
        """``policy_now``'s grants of the record systems decided over the units the store
        holds, with no live BC company listing requested: a scheduled run's reader check
        (``proactive_engine.refresh``), which must not spend a BC GET of its own."""
        return self._decide_policy(actor, None)

    def _decide_policy(self, actor: Any, bc_lookup: Any) -> ReadPolicy:
        self.tools  # noqa: B018 - built on first use
        return case_host_policy(self.runtime, actor, self.schema,
                                audience_keys=self._reader_keys, bc_lookup=bc_lookup,
                                bc_units=self._bc_units,
                                knowledge_namespaces=self.knowledge_namespaces,
                                marking_grants=self.marking_grants)

    def policy_inputs(self, actor: Any) -> tuple[tuple, tuple] | None:
        """What ``policy_now(actor)`` is decided from now, for a caller that keeps the
        decided policy (``object_workspace.HostAccess``): (values compared by equality,
        objects compared by identity, held so their identity is never reused). Any grant,
        policy, exclusion, audience or projection-generation change changes it: the gated
        actor and its entitlement's fingerprint (the employee policy's digest, identities,
        clearances, a full-read grant), the generation served, the mirror audience
        (reloaded when its file changes), the live audience, both people exclusions, the
        case mail scope and the knowledge namespace document's file state. None: not
        keyable now (the policy is decided again)."""
        self.tools  # noqa: B018 - built on first use (the generation served)
        try:
            gate = getattr(self.runtime, "employee_gate", None)
            entitlement = gate.entitlement(actor) if gate is not None else None
            mirror = self.runtime.mirror._audience()
        except Exception:  # noqa: BLE001 - undecidable: never kept
            return None
        from .pg.permissions import load_exclusion
        values = (actor.tenant, tuple(sorted(actor.principals)), actor.clearance,
                  None if entitlement is None else (entitlement.fingerprint or None),
                  entitlement is None, self._store_identity,
                  None if self._overlay is None else self._overlay.stamp,
                  _file_stamp(self.knowledge_namespaces))
        if entitlement is not None and not entitlement.fingerprint:
            return None   # an entitlement that names no fingerprint: never kept
        objects = (self._reader, mirror,
                   getattr(getattr(self.runtime, "live", None), "audience", None),
                   getattr(self.runtime, "people_exclusion", None),
                   getattr(self.runtime, "mail_scope", None), load_exclusion())
        return values, objects

    @contextmanager
    def policy_pinned(self, actor: Any) -> Iterator[None]:
        """One answer's reads (code's own, ``fast_answer.Reads``: the fast path, the
        profit route) under one policy, as the console pins one per request
        (``object_workspace.HostAccess``): ``policy_now(actor)`` decided once here is every
        call's of this context for that same actor (``policy_for``), at most
        ``POLICY_PIN_SECONDS``. As the console's, a transient answer in it (a BC company
        listing refused: ``policy_settled`` False) holds for this one answer too, never
        past it: it only ever grants less. A grant revoked during the answer applies from
        the next answer. Nested: the outer pin holds. Another actor's call never reads
        it."""
        if self._pin is not None:
            yield
            return
        policy = self.policy_now(actor)
        self._pin = (actor, policy, time.monotonic() + POLICY_PIN_SECONDS)
        try:
            yield
        finally:
            self._pin = None

    def policy_for(self, actor: Any) -> ReadPolicy:
        """The policy for ``actor`` this call reads under: the answer's pinned one
        (``policy_pinned``) while it holds for this actor, else ``policy_now``."""
        pin = self._pin
        if pin is not None and pin[0] == actor:
            if time.monotonic() < pin[2]:
                return pin[1]
        return self.policy_now(actor)

    def policy_settled(self, actor: Any) -> bool:
        """Whether the policy just decided for ``actor`` holds no transient answer: a BC
        company listing that was refused or unreadable is read again on the next call
        (``BcLookup.companies``), so such a policy is never kept."""
        lookup = self._bc_lookup
        kept = getattr(lookup, "listed", None)
        return lookup is None or (callable(kept) and kept(actor) is True)

    @property
    def evidence(self) -> Any:
        """``plx.identity``/``plx.documents``/``plx.read`` over the same store and policy,
        reading citations through the runtime (built on first use)."""
        if self._evidence is None:
            from .ontology_tools_evidence import EvidenceTools, RuntimeCitations
            tools = self.tools

            def store_lineage(handle: str) -> str | None:
                layered = self._layer_lineage(handle)
                if layered is not None:
                    return layered
                with tools.store.lock:
                    row = tools.store.conn.execute(
                        "SELECT lineage FROM object_versions WHERE l1_handle = ? LIMIT 1",
                        (handle,)).fetchone()
                return row[0] if row else None

            self._evidence = EvidenceTools(tools, reader=RuntimeCitations(
                self.runtime, mail_capture_dir=self.mail_capture_dir,
                store_lineage=store_lineage, bc_books=self.bc_books))
        return self._evidence

    def register_tool(self, name: str, handler: Any) -> None:
        """Serve one more tool through ``call`` (e.g. ``plx.act``,
        ``operating.action_tools.register``, via ``register_tool``): ``handler(arguments, session_actor)``. It
        receives this host's session actor, which every runtime store gates itself
        (``gate_actor_methods``), never the employee-gated one. Every name ``call``
        already serves (the ontology, answer and evidence tools and every ``table``
        tool: plx.around, plx.timeline, plx.process, plx.investigate, ...) and an
        already registered one is refused, so nothing shadows a host tool. Listing it on
        a host is the host's choice."""
        from .ontology_tools_aggregate import TOOL_NAMES as ANSWER_TOOLS
        from .ontology_tools_evidence import TOOL_NAMES as EVIDENCE_TOOLS
        if (not isinstance(name, str) or not callable(handler) or name in self._registered
                or name in table_tool_names() or name in ANSWER_TOOLS
                or name in EVIDENCE_TOOLS):
            raise ValueError(f"tool_not_registrable:{name}")
        self._registered[name] = handler

    @property
    def registered(self) -> frozenset[str]:
        return frozenset(self._registered)

    def call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        """One ontology tool call (``plx.schema``/``plx.find``/``plx.object``,
        ``plx.aggregate``/``plx.compare`` (operating/ontology_tools_aggregate.py), or
        ``plx.identity``/``plx.documents``/``plx.read``, or ``table``'s
        ``plx.around``/``plx.timeline``/``plx.process``/``plx.investigate``) under the policy decided for
        this call (``actor``: the employee-gated session actor). An answer read while the
        review store could not be read (``build_steps.ReviewsIfPresent``: read as no
        reviews) says so: ``review_store_unreadable`` as its first gap and its reason of
        being incomplete (``_review_store_gap``). A projection of another store format (an
        older build) answers the typed error ``rebuild_required`` until it is rebuilt."""
        unreadable = getattr(self.reviews, "unreadable_reads", None)
        before = unreadable()[0] if unreadable is not None else 0
        with self.request():
            result = self._call(name, arguments, actor)
            if unreadable is not None and unreadable()[0] > before:
                contracts = self.tools.contracts
                result = _review_store_gap(result, unreadable()[1],
                                           contracts["tools"].get(name),
                                           contracts["limits"]["max_result_bytes"])
            return result

    def _call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        from .ontology_tools_aggregate import TOOL_NAMES as ANSWER_TOOLS, AggregateTools
        from .ontology_tools_evidence import TOOL_NAMES as EVIDENCE_TOOLS
        tools = self.tools
        stale = tools.store.format_problem()
        if stale is not None:   # an older projection: never read under this code's queries
            return _error("rebuild_required", stale, "rebuild the ontology projection (a "
                          "host rebuilds it on its next capture cycle)")
        if name in self._registered:
            return self._registered[name](arguments, self.actor)
        self._check_overlay()
        tools.policy = self.policy_for(actor)
        if name in ANSWER_TOOLS:
            if self._answers is None or self._answers.base is not tools:
                self._answers = AggregateTools(tools)
            return self._answer_call(name, arguments, actor)
        if name in EVIDENCE_TOOLS:
            return self._evidence_call(name, arguments, actor)
        if name in self.table:  # plx.around/timeline/process/investigate (and the base)
            return self.table[name]["handler"](arguments, actor)
        return tools.call(name, arguments, actor)

    # ------------------------------------------------------------ row-delta overlay

    def _drop_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.close()
        self._overlay, self._overlay_stamp = None, None
        self._reader_keys = list(self._base_reader_keys)

    def _check_overlay(self) -> None:
        """Once per request: (re)open the row-delta overlay when its pointer file changed
        (``delta_overlay.open_overlay``), else keep the one decided. The policy is decided
        over the served store's audience keys and the overlay's (a unit only the delta
        holds is decided the same way: denied unless granted). Never raises: an overlay
        that cannot be opened is not applied."""
        if self._overlay_checked or self.store_path is None or self._tools is None:
            return
        if getattr(self._reader, "text_reader", False) is not False:
            return   # the Postgres ontology holds every delta layer itself
        self._overlay_checked = True
        from . import delta_overlay
        stamp = None
        try:
            pointer = delta_overlay.pointer_path(self.store_path)
            stamp = delta_overlay.pointer_stamp(pointer)
            if stamp == self._overlay_stamp:
                return
            self._drop_overlay()
            self._overlay_stamp = stamp
            if stamp is None:
                return
            overlay = delta_overlay.open_overlay(pointer, stamp, self._tools)
            if overlay is None:
                return
            keys = sorted(set(self._base_reader_keys) | set(overlay.audience_keys()))
            self._overlay = overlay
            self._reader_keys = keys
        except Exception:  # noqa: BLE001 - the served generation answers alone
            self._drop_overlay()
            self._overlay_stamp = stamp   # tried again when the pointer changes

    def _overlaid(self, name: str, arguments: Any, actor: Any, served: Any) -> dict[str, Any]:
        """``plx.object``/``plx.find`` (``served``: the table's handler over the served
        store) with the row-delta overlay applied: an owned object is read from the
        overlay, a find's owned candidates come from it (``delta_overlay.merge_find``).
        The overlay's tools read under this call's policy object. Any overlay failure
        serves the served store's answer."""
        overlay = self._overlay
        if overlay is None or self._tools is None:
            return served(arguments, actor)
        base_tools = self._tools
        from . import delta_overlay
        try:
            for layer in overlay.layers:
                layer.tools.policy = base_tools.policy
                layer.tools.clock = base_tools.clock
                layer.tools.resolves_entities = False
            if name == "plx.object":
                layer = overlay.owner(arguments.get("object_id")
                                      if isinstance(arguments, dict) else None)
                if layer is not None:
                    result = layer.tools.call(name, arguments, actor)
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, dict) else error
                    if code not in ("store_unavailable", "contract_violation"):
                        return result
                return served(arguments, actor)
            if name in ("plx.around", "plx.timeline"):
                layered = self._layer_traverse(overlay, name, arguments, actor)
                return layered if layered is not None else served(arguments, actor)
        except Exception:  # noqa: BLE001 - the served generation answers alone
            return served(arguments, actor)
        result = served(arguments, actor)
        if name != "plx.find" or not isinstance(arguments, dict) \
                or arguments.get("mode") == "entities" or "text" in arguments \
                or not delta_overlay.mergeable(result):
            # an answer no layer can change (an entity resolution, an error, a home
            # card): the layers are not asked (measured 2026-11-06: one find of a
            # question's names resolved them once per layer, 5 times over)
            return result
        try:
            answers = list(zip(_each_layer(overlay.layers, lambda layer: layer.tools.call(
                name, arguments, actor)), (layer.serves for layer in overlay.layers)))
            merged = delta_overlay.merge_find(result, answers, overlay.owned, arguments)
        except Exception:  # noqa: BLE001 - the served answer stands
            merged = None
        if merged is None:
            return result
        for candidate in (merged, delta_overlay.with_unmerged_gap(result)):
            if candidate is not None and base_tools._checked(name, candidate) is candidate:
                return candidate
        return result

    def _layer_traverse(self, overlay: Any, name: str, arguments: Any, actor: Any
                        ) -> dict[str, Any] | None:
        """``plx.around``/``plx.timeline`` from an object a delta layer serves, answered
        by that layer (its own links are whole there: the delta projects an owned object
        with every link partner) under this call's policy and item gate. A path of more
        than one hop is answered for its first hop only, with a
        ``delta_overlay_hops_deferred`` gap: the hops past it may need links the layer
        holds only in part, read at the next generation. None: the root is no layer's (or
        the layer could not answer): the served store answers."""
        from . import delta_overlay
        root = arguments.get("from") if isinstance(arguments, dict) else None
        layer = overlay.owner(root)
        if layer is None:
            return None
        path = arguments.get("path")
        deferred = name == "plx.around" and isinstance(path, list) and len(path) > 1
        asked = dict(arguments, path=path[:1]) if deferred else arguments
        result = layer.traverse.call(name, asked, actor)
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else error
        if code in ("store_unavailable", "contract_violation"):
            return None
        if deferred and "error" not in result:
            marked = delta_overlay.with_gap(result, {
                "state": "stale", "reason": delta_overlay.HOPS_DEFERRED,
                "subject": f"plx.around:{root}"[:700],
                "detail": f"this record changed since the served projection generation: "
                          f"its first hop is read as it is now; the {len(path) - 1} further "
                          f"hop(s) of the path are read at the next generation"})
            if marked is None or layer.traverse._checked(name, marked) is not marked:
                return None
            result = marked
        return result

    def _owned_readable(self, name: str, arguments: Any, actor: Any) -> int | None:
        """How many objects a delta layer serves, that ``actor`` may read, are among
        those a ``plx.aggregate``/``plx.compare`` call reads: its set's or its two sides'
        objects, or (a filter or type aggregate) a layer's matches of its filters (up to
        one page each). Only a count, never their content. None: not decidable."""
        overlay = self._overlay
        if overlay is None or not isinstance(arguments, dict):
            return 0
        tools = self._tools
        for layer in overlay.layers:
            layer.tools.policy, layer.tools.clock = tools.policy, tools.clock
        if name == "plx.compare":
            ids = [side for side in (arguments.get("left"), arguments.get("right"))
                   if isinstance(side, str)]
        elif isinstance(arguments.get("set"), str):
            ids = list(tools.sets.resolve(arguments["set"], actor) or ())
        else:
            if not arguments.get("object_type") and not arguments.get("filters"):
                return 0
            asked = {key: arguments[key] for key in ("object_type", "filters")
                     if key in arguments}
            count = 0
            for layer in overlay.layers:
                found = layer.tools.call("plx.find", dict(asked, limit=50), actor)
                if "error" in found:
                    return None
                count += sum(1 for c in found.get("candidates", ())
                             if c["object"]["object_id"] in layer.serves)
            return count
        return sum(len(layer.readable(actor, ids)) for layer in overlay.layers)

    def _answer_call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        """``plx.aggregate``/``plx.compare`` over the served store, saying when objects
        changed since (read from a delta layer elsewhere) are among those it read
        (``delta_overlay_not_aggregated``, with their count among those this reader
        may read): the totals are the served generation's."""
        result = self._answers.call(name, arguments, actor)
        if self._overlay is None or "error" in result:
            return result
        from . import delta_overlay
        try:
            count = self._owned_readable(name, arguments, actor)
        except Exception:  # noqa: BLE001 - undecidable: said as such
            count = None
        if count == 0:
            return result
        detail = ("the totals read the served projection generation; "
                  + (f"{count} record(s) changed since are not in them" if count is not None
                     else "records changed since may not be in them"))
        marked = delta_overlay.with_gap(result, {
            "state": "stale", "reason": delta_overlay.NOT_AGGREGATED, "subject": name,
            "detail": detail})
        if marked is not None and self._answers._checked(name, marked) is marked:
            return marked
        return result

    def _layer_lineage(self, handle: str) -> str | None:
        """The lineage the newest delta layer serving ``handle``'s object records for it,
        else None (the served store's is read)."""
        overlay = self._overlay
        if overlay is None:
            return None
        try:
            for layer in overlay.layers:
                with layer.reader.lock:
                    rows = layer.reader.conn.execute(
                        "SELECT object_id, lineage FROM object_versions WHERE l1_handle = ?",
                        (handle,)).fetchall()
                for object_id, lineage in rows:
                    if object_id in layer.serves:
                        return lineage
        except Exception:  # noqa: BLE001 - the served store's lineage answers
            return None
        return None

    def _evidence_call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        """``plx.identity``/``plx.documents``/``plx.read`` with the row-delta overlay:
        ``plx.identity`` of objects one layer serves and ``plx.documents`` of an object a
        layer serves are answered by that layer's evidence tools (under this call's policy,
        item gate and citation reader); ``plx.identity`` mixing served and layered objects
        answers from the served store with a ``delta_overlay_unmerged`` gap. ``plx.read``
        reads its citation from the source (a layer's lineage first: ``_layer_lineage``).
        Any overlay failure serves the served store's answer."""
        served = self.evidence
        overlay = self._overlay
        if overlay is None or name == "plx.read" or not isinstance(arguments, dict):
            return served.call(name, arguments, actor)
        from . import delta_overlay
        if name == "plx.identity":
            ids = arguments.get("object_ids")
            owners = ([overlay.owner(i) for i in ids] if isinstance(ids, list) else [None])
        else:
            owners = [overlay.owner(arguments.get("object_id"))]
        if all(owner is None for owner in owners):
            return served.call(name, arguments, actor)
        layer = owners[0]
        if layer is not None and all(owner is layer for owner in owners):
            try:
                layer.tools.policy = self._tools.policy
                layer.tools.clock = self._tools.clock
                if getattr(layer, "evidence", None) is None:
                    from .ontology_tools_evidence import EvidenceTools
                    layer.evidence = EvidenceTools(layer.tools, reader=served.reader)
                layer.evidence.reader = served.reader
                result = layer.evidence.call(name, arguments, actor)
                error = result.get("error")
                code = error.get("code") if isinstance(error, dict) else error
                if code not in ("store_unavailable", "contract_violation"):
                    return result
            except Exception:  # noqa: BLE001 - the served generation answers alone
                pass
        result = served.call(name, arguments, actor)
        marked = delta_overlay.with_unmerged_gap(result, subject=name)
        if marked is not None and served._checked(name, marked) is marked:
            return marked
        return result

    @contextmanager
    def per_call(self) -> Iterator[None]:
        """One tool call's per-call state: none here (``CaptureCaseHostTools`` keeps its
        capture gates' and live re-reads')."""
        yield None

    def not_found_reason(self, object_id: str, actor: Any) -> str | None:
        """Why ``plx.object`` answers ``object_not_found`` for ``object_id`` and ``actor``
        (employee-gated) now, decided by the same reads under the policy decided for this
        call: the object's audience unit, its version current now and the markings of that
        and of its latest version (``StoreReader.visible_versions``), then the item gate
        through the version's L1 handle. ``absent``: the store holds no such object;
        ``no_current_version``: no version is current now even with every grant and
        marking (all superseded, recorded later, or a time that does not parse);
        ``withheld``: one is, and the reader's grant, a marking it lacks or its item gate
        refusing it hides it; ``gate_error``: the item gate raised. None: the reader may
        read it. An operator's diagnosis only: never put in a reader's answer, where it
        would tell a withheld object from an absent one."""
        with self.request(), self.per_call():
            tools = self.tools
            tools.policy = self.policy_now(actor)
            with tools.store.lock:
                scope = tools._scope(actor, tools._as_known_at({}))
                found = tools.store.objects([object_id]).get(object_id)
                if found is None:
                    return "absent"
                from ontology.store_ddl import MARKING_SETS
                every = dataclasses.replace(scope, audience_keys=(found["audience_key"],),
                                            marking_sets=MARKING_SETS)
                if not tools.store.visible_versions(every, [object_id]):
                    return "no_current_version"
                versions = tools.store.visible_versions(scope, [object_id])
            if not versions:
                return "withheld"
            for version in versions:
                handle = version["l1_handle"]
                if handle.split("://", 1)[0] not in _ITEM_ACL_SCHEMES:
                    continue
                try:
                    admitted = tools.item_gate is not None and tools.item_gate(actor, handle)
                except Exception:  # noqa: BLE001 - plx.object reads a raising gate as a refusal
                    return "gate_error"
                if admitted is not True:
                    return "withheld"
            return None

    def register(self, tool_table: dict[str, Any]) -> None:
        """Add ``plx.identity``/``plx.documents``/``plx.read`` to an in-process tool
        table (name -> spec with ``handler(arguments, actor)``), each call through
        ``call`` so the policy is decided again per call. A name already present is
        refused."""
        from .ontology_tools_evidence import tool_specs
        for spec in tool_specs(self.tools.contracts):
            name = spec["name"]
            if name in tool_table:
                raise ValueError(f"tool already registered: {name}")
            tool_table[name] = dict(spec, handler=(
                lambda arguments, actor, _n=name: self.call(_n, arguments, actor)))

    def open_generation(self) -> None:
        """Open the projection generation this host reads now: build its tool table and,
        with it, read the store facts the traversal tools share between readers
        (``TraverseTools.prime``). A host calls this when it starts serving; a request
        that finds a newer generation does it again before its call (``request``
        dropped the table). Never raises for an unreadable store."""
        if self.store_path is None:
            return
        with self.request():
            self._open_table()

    def _open_table(self) -> None:
        try:
            self.table  # noqa: B018 - built on first use
        except Exception:  # noqa: BLE001 - the call reports it, as without this
            self.close()   # what a failed open built: the call builds it again

    def _document_read(self, handle: str, actor: Any) -> dict[str, Any]:
        """``plx.read`` of one citation for ``actor`` (the employee-gated actor of the call
        that runs the engine, under the policy decided for that call): the text a
        ``plx.investigate`` engine reads (``operating.vendor_terms``), never more than
        one window of it."""
        return self.evidence.call("plx.read", {"citation": handle, "max_chars": 20000}, actor)

    @property
    def table(self) -> dict[str, dict[str, Any]]:
        """``tool_table`` over ``tools`` and this runtime: plx.schema/find/object,
        plx.around/timeline, plx.process and plx.investigate (its plans dispatch through
        this same table). Built on first use."""
        if self._table is None:
            table = tool_table(self.tools, self.runtime, schema=self.schema,
                               follow_ups=self.follow_ups, label_reserve=self.label_reserve,
                               documents=self._document_read)
            # plx.find/plx.object read the row-delta overlay too, whether called by a
            # reader or dispatched by a plan of plx.investigate through this table.
            for name in ("plx.find", "plx.object", "plx.around", "plx.timeline"):
                spec = table.get(name)
                if spec is not None:
                    spec["handler"] = (lambda arguments, actor, _n=name, _h=spec["handler"]:
                                       self._overlaid(_n, arguments, actor, _h))
            self._table = table
        return self._table


__all__ = ["NOT_FOUND_REASONS", "CaseHostTools", "case_host_policy", "check_marking_grants",
           "live_lookups", "store_bc_units", "table_tool_names", "tool_table"]
