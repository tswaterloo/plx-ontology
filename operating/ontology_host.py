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
the runtime's own audience-checked readers (``RuntimeCitations``). The MCP listing of these
three tools is the host budget's (case-host 22-tool budget).

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

from contextlib import contextmanager
import dataclasses
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Iterator, Mapping

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
    from . import ontology_tools_investigate, ontology_tools_process, ontology_tools_traverse
    return frozenset(TOOL_NAMES) | frozenset(ontology_tools_traverse.TOOL_NAMES) | frozenset(
        ontology_tools_process.TOOL_NAMES) | frozenset(ontology_tools_investigate.TOOL_NAMES)


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
               label_reserve: int = 0) -> dict[str, dict[str, Any]]:
    """The generic tools over ``tools`` as one host tool table (name -> spec with
    ``handler(arguments, actor)``): plx.schema/find/object, plx.around/timeline,
    plx.process and plx.investigate, which dispatches its plans through this same table
    (so a three_way_match plan gets plx.process's verdicts) and checks its claims
    against the runtime's per-reader citation reads (``CaseRuntime.resolve_citation``) and
    proves a linked set whole only within the reader's policy (``read_scope``).
    ``follow_ups``: plx.investigate's follow-up recorder
    (``ontology_tools_investigate.follow_up_recorder``; none: the option is refused).
    ``label_reserve``: the bytes the host's labels add to a result after plx.investigate
    answers (``InvestigateTools``), kept free by its pages. Every handler reads under
    ``tools.policy`` as the caller set it for this call."""
    from . import (ontology_tools, ontology_tools_investigate, ontology_tools_process,
                   ontology_tools_traverse)
    table: dict[str, dict[str, Any]] = {}
    ontology_tools.register(table, tools)
    traverse = ontology_tools_traverse.TraverseTools(tools)
    # The store facts every reader's plx.around / plx.timeline needs, read once per store
    # generation as the table is built over it (a served generation is a file never written
    # again; a newer one gets a new table), so no reader's call pays for them.
    traverse.prime()
    ontology_tools_traverse.register(table, traverse)
    ontology_tools_process.register(table, _LazyProcess(tools))
    ontology_tools_investigate.register(table, ontology_tools_investigate.InvestigateTools(
        table, contracts=tools.contracts, schema=schema if schema is not None else tools.schema,
        rows=ontology_tools_investigate.citation_rows(runtime),
        scope=ontology_tools_investigate.read_scope(tools),
        captured=ontology_tools_investigate.capture_claims(tools),
        complete=ontology_tools_investigate.relation_complete(tools), follow_ups=follow_ups,
        reviews=traverse.review, label_reserve=label_reserve))
    return table


def live_lookups(runtime: Any, *, bc: bool = True,
                 bc_books: Mapping[str, str] | None = None,
                 max_staleness: float | None = None) -> dict[str, Any]:
    """``SourceLookup`` per source family over the runtime's own readers (GET only);
    ``max_staleness`` bounds the age of the mirror pages the Portal lookup answers from."""
    from ontology.resolve import BcLookup, MirrorLookup
    lookups: dict[str, Any] = {"portal_api": MirrorLookup(runtime.mirror,
                                                          max_staleness=max_staleness)}
    live = getattr(runtime, "live", None)
    if bc and live is not None and live.bc is not None:
        lookups["bc"] = BcLookup(live.bc, actor_for=runtime.employee_gate, books=bc_books)
    return lookups


def store_bc_units(reader: StoreReader) -> dict[str, str]:
    """BC capture units a store holds (``bc:<company>/<entitySet>``) -> the BC
    environment their capture handles name (``bc://<environment>/...``)."""
    units: dict[str, str] = {}
    with reader.lock:
        rows = reader.conn.execute(
            "SELECT o.audience_key, MIN(v.l1_handle) FROM objects o JOIN object_versions v "
            "ON v.object_id = o.object_id WHERE o.audience_key LIKE 'bc:%' "
            "AND v.l1_handle LIKE 'bc://%' GROUP BY o.audience_key").fetchall()
    for key, handle in rows:
        units[key] = handle[len("bc://"):].split("/", 1)[0]
    return units


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
    or malformed). Exact keys only, so no family counts as fully covered. A marking is
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
    return ReadPolicy(actor.tenant, grants, dict(marking_grants or {}))


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


def _file_identity(path: Path) -> tuple[int, int] | None:
    """The file ``path`` names now, or None. A generation is published by renaming a
    new file over the path and never written again, so the inode tells them apart."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_dev, found.st_ino)


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
                 marking_grants: Mapping[str, Iterable[str]] | None = None) -> None:
        self.runtime, self.actor = runtime, actor
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
        self._bc_units: dict[str, str] = {}
        self._bc_lookup: Any = None
        self._reader: StoreReader | None = None
        self._store_identity: tuple[int, int] | None = None  # the file the reader opened
        self._depth = 0  # requests in progress (``request``)
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
        if self._reader is not None:
            self._reader.conn.close()
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
        thread)."""
        swapped = False
        if self._depth == 0 and self._reader is not None and self.store_path is not None:
            now = _file_identity(self.store_path)
            if now is not None and now != self._store_identity:
                self.close()
                swapped = True
        self._depth += 1
        try:
            if swapped:
                self._open_table()
            yield self
        finally:
            self._depth -= 1

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
            reader = self._reader = StoreReader.open(store)
            lookups = {source: lookup for source, lookup in
                       live_lookups(self.runtime, bc=self.bc, bc_books=self.bc_books,
                                    max_staleness=self.max_staleness).items()
                       if source != self.exclude_source}
            self._reader_keys = reader.audience_keys()
            self._bc_units = store_bc_units(reader)
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
                or getattr(self.runtime, "client_configured", True) else ("portal_api",))
            # The empty placeholder store is not a projection: filter/listing finds
            # on it report projection_not_built and complete=false.
            self._tools.projection_built = self.store_path is not None
        return self._tools

    def policy_now(self, actor: Any) -> ReadPolicy:
        """The policy for ``actor`` (already through the employee gate) decided now from
        the readers' audiences: a revoked grant applies to the next call."""
        self.tools  # noqa: B018 - built on first use
        return case_host_policy(self.runtime, actor, self.schema,
                                audience_keys=self._reader_keys, bc_lookup=self._bc_lookup,
                                bc_units=self._bc_units,
                                knowledge_namespaces=self.knowledge_namespaces,
                                marking_grants=self.marking_grants)

    @property
    def evidence(self) -> Any:
        """``plx.identity``/``plx.documents``/``plx.read`` over the same store and policy,
        reading citations through the runtime (built on first use)."""
        if self._evidence is None:
            from .ontology_tools_evidence import EvidenceTools, RuntimeCitations
            tools = self.tools

            def store_lineage(handle: str) -> str | None:
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
        tools.policy = self.policy_now(actor)
        if name in ANSWER_TOOLS:
            if self._answers is None or self._answers.base is not tools:
                self._answers = AggregateTools(tools)
            return self._answers.call(name, arguments, actor)
        if name in EVIDENCE_TOOLS:
            return self.evidence.call(name, arguments, actor)
        if name in self.table:  # plx.around/timeline/process/investigate (and the base)
            return self.table[name]["handler"](arguments, actor)
        return tools.call(name, arguments, actor)

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

    @property
    def table(self) -> dict[str, dict[str, Any]]:
        """``tool_table`` over ``tools`` and this runtime: plx.schema/find/object,
        plx.around/timeline, plx.process and plx.investigate (its plans dispatch through
        this same table). Built on first use."""
        if self._table is None:
            self._table = tool_table(self.tools, self.runtime, schema=self.schema,
                                     follow_ups=self.follow_ups,
                                     label_reserve=self.label_reserve)
        return self._table


__all__ = ["NOT_FOUND_REASONS", "CaseHostTools", "case_host_policy", "check_marking_grants",
           "live_lookups", "store_bc_units", "table_tool_names", "tool_table"]
