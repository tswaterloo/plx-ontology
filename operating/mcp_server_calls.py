"""The case host's served notes, binding, reads and tool calls."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from ontology.core import Actor

from . import (
    action_tools, agent_definitions, answer_gate, capability_guards, case_context_tools,
    host_peers, kind_confirmation, mcp_portfolio, ontology_tools, ontology_tools_search,
    session_identity
)
from .case_tools_host import PagedRead
from .session_identity import SessionIdentity, SessionIdentityRefused

from .mcp_server_anchors import MAX_KEPT_FINDS, START_ANCHOR_SECONDS
from .mcp_server_catalog import (
    _HOST_READ_ARGUMENTS, _no_record_named, _with_ask_back, AGENDA_TOOLS, CASE_RUNTIME_TOOLS,
    GENERIC_TOOLS, home_kinds, HOST_READS, LATE_PO_KINDS, PLAYBOOK_TOOLS, RETURNING_TOOLS
)
from .mcp_server_evidence import (
    _drop_changed, _evidence_items, _evidence_scope, _ItemPermissions, _merged, _served_texts,
    _text_key, _unkept_reason, MAX_SERVED_SPANS, MAX_SERVED_TEXTS, MAX_TEXT_HANDLES,
    PERMISSIONS_UNREADABLE, UNRESTRICTED_SCOPE
)
from .mcp_server_results import _FRESHNESS_UNIT_FIELDS, InputError


class CallsMixin:
    """Served notes, binding, reads and tool calls."""

    @property
    def actor(self) -> Actor:
        """The actor this session serves: its session identity's when the host set one
        (an expired session ticket refuses every further read)."""
        if self.session is not None and self.session.expired():
            raise SessionIdentityRefused("session_expired")
        return self._actor

    def rebind(self, session: SessionIdentity) -> None:
        """Bind this request's verified session (a transport that authenticates every
        request: ``operating.mcp_http_host``): the same employee with fresh proof and
        expiry, never another actor or a fixture identity."""
        if self.session is None or self.session.fixture or type(session) is not SessionIdentity \
                or session.fixture or session.channel != session_identity.CHANNEL_MCP \
                or session.actor != self._actor:
            raise ValueError("mcp_session_rebind_invalid")
        self.session = session
        self._act = action_tools.ActTool(self.case_runtime, self.action_path,
                                         reads=self._action_reads,
                                         test_capabilities=self.test_capabilities,
                                         schema=self._ontology_schema, session=session)

    @staticmethod
    def _owner_open(scope: str | None, reads: _ItemPermissions | None) -> bool:
        """Whether ``reads`` were skipped for an unrestricted reader and the scope this
        call's evidence is kept under was decided for one too (``_evidence_scope``): then
        nothing is bound to, or dropped for, an item permission. A grant that changed in
        between (one says unrestricted, the other not) is neither: undecided, so what any
        item gates is dropped as for a failed read (fail closed)."""
        return reads is not None and reads.unrestricted and isinstance(scope, str) \
            and scope.startswith(UNRESTRICTED_SCOPE)

    def _keep_items(self, reads: _ItemPermissions) -> bool:
        """Note, of each item the kept evidence names that ``_items`` lacks (what this call
        returned), its digest at the snapshot this call read under (taken before it), and
        every item's when a change to any decides kept evidence (``_evidence_items``), so a
        change since is seen at the next comparison (``_bind``); False when they cannot be
        read (the caller drops what any item gates)."""
        if reads.unrestricted:
            # Skipped for an unrestricted reader, but this call's scope is not one
            # (``_owner_open``): nothing was read, so nothing binds (fail closed).
            self._unkept, self._owner = PERMISSIONS_UNREADABLE, None
            return False
        named, whole = _evidence_items(self._evidence)
        try:
            added = reads.digests(named - self._items.keys())
            if whole and self._whole is None:
                self._whole = reads.whole()
        except Exception as exc:  # noqa: BLE001 - an undecided read binds nothing (fail closed)
            self._unkept = _unkept_reason(exc)
            return False
        self._items = {key: digest for key, digest in self._items.items() if key in named}
        self._items.update(added)
        return True

    def _note_texts(self, shown: Any) -> None:
        """Keep how much of each text a ``plx.read`` result served this session's reader
        (``_served_texts``): whole, or the character spans it served of a text whose
        length the window said, per record and field whatever alias the read named
        (``_text_key``). A read of another version starts the text over. A text is whole
        once one read served it whole or its spans cover it; until then a claim citing
        it makes the answer partial (``unread``)."""
        texts, handles = self._evidence["texts"], self._evidence["handles"]
        for cited, field, version, served in _served_texts(shown):
            for key in dict.fromkeys(_text_key(handle, field) for handle in cited):
                entry = texts.pop(key, None)
                if entry is None or entry["version"] != version:
                    entry = {"version": version, "whole": False, "spans": [],
                             "parts": [], "total": None,
                             "handles": [] if entry is None else entry["handles"]}
                texts[key] = entry
                for handle in cited:
                    if handle not in entry["handles"] \
                            and len(entry["handles"]) < MAX_TEXT_HANDLES:
                        entry["handles"].append(handle)
                if served is True:
                    entry["whole"] = True
                    continue
                start, end, total, sha = served
                if total is not None:
                    entry["total"] = total
                if sha is not None and end > start \
                        and len(entry["parts"]) < MAX_SERVED_SPANS \
                        and (start, end, sha) not in entry["parts"]:
                    entry["parts"].append((start, end, sha))
                if end > start and len(entry["spans"]) < MAX_SERVED_SPANS:
                    entry["spans"] = _merged([*entry["spans"], (start, end)])
                total = entry["total"]
                if total is not None and entry["spans"] and entry["spans"][0][0] == 0 \
                        and entry["spans"][0][1] >= total:
                    entry["whole"] = True
        while len(texts) > MAX_SERVED_TEXTS:
            entry = texts.pop(next(iter(texts)))
            if not entry["whole"]:
                for handle in entry["handles"]:   # fail closed: forgetting is never whole
                    handles.pop(handle, None)

    def read_texts(self) -> list[tuple[list[str], str, list[tuple[int, int]] | None]]:
        """The texts ``plx.read`` served this session's reader under the scope in force
        (``_note_texts``), newest first: ``(handles, field, spans)``, the handles that
        name the text, ``spans`` the
        character spans served of it, None when it was served whole. What the answer gate
        may bind a reply's figure to (``answer_gate.bind_read_figures``)."""
        out = []
        for key, entry in reversed(list(self._evidence.get("texts", {}).items())):
            field = key[-1] if isinstance(key, tuple) and key else None
            if not isinstance(field, str) or not entry.get("handles"):
                continue
            spans = None if entry.get("whole") else [tuple(span) for span in entry["spans"]]
            if spans == []:
                continue
            out.append((list(entry["handles"]), field, spans))
        return out

    def served_parts(self, handle: str, field: str) -> list[tuple[int, int, str]]:
        """The parts of a partly served text (``unread``) this session's reader was
        served, each ``(start, end, sha256 of those characters)``: the answer gate counts
        a part only where the text it re-reads holds the same characters there
        (``answer_gate._in_full``). None known, or the text served whole: []."""
        found = self._served_entries(handle, field)
        if not found or any(entry["whole"] for entry in found):
            return []
        return [part for entry in found for part in entry.get("parts", ())]

    def unread(self, handle: str, field: str) -> bool:
        """Whether this session's reader was served only part of ``field`` of the record
        ``handle`` names by ``plx.read`` (``_note_texts``), under any alias of it: the
        handle's own record (``_text_key``), else the record the claim gate's re-read of
        it names (``CaseRuntime.claim_row``'s citation, read only while some text is
        partly served). Partial when such a text was served in part and none whole."""
        found = self._served_entries(handle, field)
        return bool(found) and not any(entry["whole"] for entry in found)

    def _served_entries(self, handle: str, field: str) -> list[dict[str, Any]]:
        """What ``_note_texts`` keeps of ``field`` of the record ``handle`` names, under
        any alias of it (``unread``); none while every served text is whole."""
        texts = self._evidence.get("texts", {})
        if not isinstance(handle, str) or not isinstance(field, str) \
                or all(entry["whole"] for entry in texts.values()):
            return []
        keys = {_text_key(handle, field)}
        if not keys & texts.keys():
            try:
                view = self.case_runtime.claim_row(handle, self.actor)
            except Exception:  # noqa: BLE001 - unreadable now: the gate refuses the claim
                view = None
            if isinstance(view, dict):
                keys |= {_text_key(alias, field)
                         for alias in (view.get("citation"), view.get("l1_handle"))
                         if isinstance(alias, str)}
        return [texts[key] for key in keys if key in texts]

    def _cleared_note(self, name: str, arguments: Any) -> str:
        """For a ``plx.case_get`` answer submission the gate could not decide (an error,
        a result too large): when it gives another value for a claim this session's last
        decision passed (``answer_gate.contradicted_claims``), that decision is cleared
        and the agent is told so with this error (a repair turn); else nothing (the
        earlier decision stands: the submission decided nothing)."""
        if name != "plx.case_get" or not isinstance(arguments, dict) \
                or not isinstance(arguments.get("answer"), dict) or self._last_decision is None:
            return ""
        claims = answer_gate.contradicted_claims(self._last_decision,
                                                 arguments["answer"].get("claims"))
        if not claims:
            return ""
        self._last_decision = None
        return answer_gate.CLEARED_BY_CONTRADICTION.format(
            count=len(claims), claims=", ".join(claims[:8])) + "\n"

    def _told_question(self, params: Any) -> None:
        """The gate's notice of the session's question (``host_peers.QUESTION_METHOD``),
        at the session's start: the question's anchors resolved now, once, never at its
        submission (``_question_anchors``): from ``entities`` when the app resolved them
        before the session (no search), else by one search within
        ``START_ANCHOR_SECONDS``."""
        if not isinstance(params, Mapping) or not isinstance(params.get("question"), str):
            return
        asked = params["question"].strip()
        if not asked or self.ontology is None:
            return
        given = params.get("entities")
        entities = [entity for entity in given if isinstance(entity, Mapping)] \
            if isinstance(given, list) else None
        self._resolve_anchors(asked, entities, START_ANCHOR_SECONDS)

    def _kept_find(self, arguments: Any, built: Any) -> None:
        """Keep a ``plx.find`` this session made (its arguments and result) for the
        relevance check (``_found_on_question``), at most ``MAX_KEPT_FINDS``."""
        if isinstance(arguments, Mapping) and isinstance(built, Mapping) \
                and len(self._finds) < MAX_KEPT_FINDS:
            self._finds.append((dict(arguments), built))

    def _unrestricted(self) -> bool:
        """The session reader's owner-open flag (``EmployeeEntitlement.unrestricted``, the
        entitlement the employee gate resolved for this actor), decided once per session;
        anything undecidable: False (the item permissions are read as for anyone). Each
        call's read policy must agree (``_owner_open``); when it does not, the flag is
        decided again at the next call."""
        from .employee_permissions import reader_unrestricted
        if self._owner is None:
            self._owner = reader_unrestricted(getattr(self.case_runtime, "employee_gate",
                                                      None), self.actor)
        return self._owner

    def _close_reads(self) -> None:
        """End the snapshots of the item permission reads this session holds open."""
        while self._open_reads:
            try:
                self._open_reads.pop().close()
            except Exception:  # noqa: BLE001 - closing a read decides nothing
                pass

    def close(self) -> None:
        """End of session: release the case host's store and working directory."""
        self._close_reads()
        if self.ontology is not None:
            self.ontology.close()
        self._read = None

    def _action_type(self, name: str, arguments: dict[str, Any]) -> str | None:
        """The catalog action type a call would propose or run, read before the call:
        plx.case_propose's capability, plx.act propose's action or the capability of the
        recorded proposal it registers. None when the call names no catalog action type
        (an undeclared or non-canonical name, which the tool refuses on its own) or the
        case does not show the proposal to this actor."""
        if name == "plx.case_propose":
            return self._catalog_type(arguments.get("capability"))
        if name != action_tools.TOOL_NAME or arguments.get("mode") != "propose":
            return None
        if "action" in arguments:
            return self._catalog_type(arguments["action"])
        case_id, proposal_id = arguments.get("case_id"), arguments.get("proposal_id")
        if not isinstance(case_id, str) or not isinstance(proposal_id, str):
            return None
        try:
            proposals = self.case_runtime.cases.get(self.actor, case_id).get("proposals", ())
        except Exception:  # noqa: BLE001 - unreadable here is decided by the tool itself
            return None
        return self._catalog_type(next((item.get("capability") for item in proposals
                                        if item.get("id") == proposal_id), None))

    @staticmethod
    def _catalog_type(name: Any) -> str | None:
        return name if isinstance(name, str) and agent_definitions.approval_rule(name) \
            is not None else None

    def _escalation(self, name: str, built: Any) -> dict[str, Any] | None:
        """The escalation a call raised: a plx.act registration parks an owner-system
        intent for its approval rule (the agent never requests, decides or dispatches it)."""
        if name != action_tools.TOOL_NAME or not isinstance(built, dict) \
                or built.get("kind") != "owner_system_proposal":
            return None
        intent = built.get("intent") or {}
        rule = (built.get("approval_rule") or {}).get("required")
        if not isinstance(rule, str) or intent.get("withheld"):
            return None
        return self.agent_run.approval_escalation(
            rule, action_type=intent.get("capability"), intent_id=intent.get("intent_id"),
            intent_state=intent.get("state"))

    def _freshness_meta(self) -> dict[str, Any]:
        """Counts-only ingestion freshness for the session actor (``case_context_tools.
        ingest_status``: only units whose source scope the actor may read are shown,
        the rest only counted). Without --ingest-sources: ``configured`` false."""
        if self.ingestion is None:
            return {"configured": False}
        try:
            status = case_context_tools.ingest_status(self.case_runtime, self.actor,
                                                      self.ingestion)
        except Exception:  # noqa: BLE001 - freshness unknown is stated, never guessed
            return {"configured": True, "status": "unavailable"}
        return {"configured": True, "status": "read", "summary": status["summary"],
                "units": [{key: unit.get(key) for key in _FRESHNESS_UNIT_FIELDS}
                          for unit in status["units"]],
                "units_withheld": status["visibility"]["units_withheld"],
                "runs": status["runs"], "completeness_rule": status["completeness_rule"],
                "counts_scope": status["counts_scope"]}

    def _argument_names(self, name: str) -> frozenset[str]:
        """The argument names a tool declares (a refusal may name them bare)."""
        if name in HOST_READS:
            return _HOST_READ_ARGUMENTS
        return frozenset(next((tool["inputSchema"].get("properties") or {}
                               for tool in self.tools if tool["name"] == name), {}))

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == ontology_tools_search.TOOL_NAME:
            # plx.documents itself, as this session's reader, its hits cut short
            return ontology_tools_search.run(
                lambda wanted: self._generic_call(ontology_tools_search.DOCUMENTS, wanted),
                arguments,
                call_read=lambda wanted: self._generic_call(ontology_tools_search.READ, wanted),
                call_comms=lambda wanted: self._generic_call(ontology_tools_search.COMMS_SEARCH,
                                                             wanted),
                call_records=lambda wanted: self._generic_call(
                    ontology_tools_search.RECORDS_SEARCH, wanted))
        if name == ontology_tools_search.OPEN_NAME:
            # plx.read itself, as this session's reader; Freightcom never shown
            return ontology_tools_search.open_one(
                lambda wanted: self._generic_call(ontology_tools_search.READ, wanted),
                arguments,
                lambda wanted: self._generic_call(ontology_tools_search.COMMS_OPEN, wanted))
        if name in GENERIC_TOOLS or name in AGENDA_TOOLS:
            return self._generic_call(name, arguments)
        known = (_HOST_READ_ARGUMENTS if name in HOST_READS else
                 next(tool["inputSchema"] for tool in self.tools
                      if tool["name"] == name)["properties"])
        unknown = set(arguments) - set(known)
        if unknown:
            raise InputError("unknown_arguments:" + ",".join(sorted(unknown))[:128])
        if name in CASE_RUNTIME_TOOLS or name in HOST_READS:
            return self._case_runtime_call(name, arguments)
        if name in PLAYBOOK_TOOLS:
            return self._playbook_call(name, arguments)
        raise InputError("unknown_tool")

    def _playbook_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One playbook proposal tool as the session actor (``operating.playbook_tools``).
        An evaluation reads the frozen cases through ``_evaluator_reads``; a confirmed
        Workbench document kind counts for it as for any evaluating reader."""
        if self.playbook_proposals is None:
            raise InputError("playbook_proposals_not_configured: the host keeps no "
                             "playbook proposal configuration (default deny)")
        actor = self.actor
        options = {} if self.kind_confirmations is None else {
            "kind_confirmations": kind_confirmation.confirmed_kinds(self.kind_confirmations,
                                                                    actor)}
        if self.case_runtime is not None:
            # A byte-pinned document citation is read as plx.act propose reads it.
            options["document_rows"] = capability_guards.current_reader(self.case_runtime,
                                                                         actor)
        return self.playbook_proposals.call(
            name, arguments, actor, runtime=self.case_runtime, reads=self._evaluator_reads,
            evaluation_options=options, client_capabilities=self.client_capabilities,
            send_request=self.outbound.append, client_info=self.client_info)

    def _generic_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One generic ontology tool. The employee gate applies before any read, as for
        every case read; the read policy is the session's while what it is decided from is
        unchanged (``operating.kept_policy``), else decided again; the contracts
        validate the arguments and every output. Every tool but plx.read (paged here) is
        ``CaseHostTools.call``, so plx.investigate's plans run over the same table
        (plx.process included) the host serves."""
        gated = self.case_runtime.employee_gate(self.actor)
        host = self._ontology_host()
        self._read_scope = None
        # Before the call: an item re-read during it moves that item, so what the call
        # returned of it is dropped by the next comparison. Undecided: nothing this call
        # returns is kept (fail closed) and its answer says why.
        reads = self._item_reads(host)
        # One projection generation for the call and the scope its evidence is kept under.
        with host.request():
            if name != "plx.read":
                result = host.call(name, arguments, gated)
                if name == "plx.find":
                    result = self._home_mode(host, gated, arguments, result)
                    result = self._portfolio_mode(host, gated, arguments, result)
                    result = _with_ask_back(arguments, result)
                elif name == "plx.investigate":
                    self._verdict_text(host, gated, arguments, result)
            else:
                tools = host.tools
                tools.policy = host.policy_for(gated)
                if self._read is None or self._read[0] is not tools:
                    # Rebuilt if the host rebuilt its tools (a newer generation).
                    self._read = (tools, PagedRead(host.evidence, self.case_runtime))
                result = self._read[1].call(arguments, gated)
            # The policy this call read under (host.call decided it for this call);
            # undecided item permissions (None) keep only what no item gates.
            self._read_scope = (_evidence_scope(host.store_meta(), host.tools.policy), reads)
        return result

    def held_markings(self) -> frozenset[str]:
        """The value markings this session's reader holds now: the read policy its tool
        calls are decided under (``policy_for`` of the gated actor). The answer gate reads
        cited rows with them (``answer_gate._reader``)."""
        gated = self.case_runtime.employee_gate(self.actor)
        host = self._ontology_host()
        return frozenset(host.policy_for(gated).held_markings(gated))

    def _peer_export(self, name: str, arguments: dict[str, Any], built: Any,
                     shown: Any) -> dict[str, Any] | None:
        """A helper's read for the session's primary (``operating.host_peers``): the call,
        its result as built and as shown, the evidence scope it was read under and the
        item permission digests this host holds (each read before the call that first
        named its item). None when nothing of it is evidence or it is not plain JSON
        (then the primary keeps nothing of it: fail closed)."""
        if name not in RETURNING_TOOLS or self._read_scope is None \
                or not isinstance(self._read_scope[0], str):
            return None
        try:
            record = json.loads(json.dumps({
                "name": name, "arguments": arguments, "built": built, "shown": shown,
                "scope": self._read_scope[0], "kept": self._unkept is None,
                "items": [[list(key), digest] for key, digest in self._items.items()]},
                ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError):
            return None
        return record

    def _peer_import(self, params: Any) -> None:
        """Keep a helper's read as this session's evidence (``operating.host_peers``), as
        ``_note_returned`` keeps one of this host's own calls: only with this primary's
        key, under the evidence scope in force now (a read under another projection
        generation or grant keeps nothing), bound to the item permissions read now; what
        an item gates whose permissions the helper read otherwise before its call is
        dropped, and anything undecidable drops what any item gates (fail closed). The
        call counts against this session's agent run like one served here."""
        if self.peer_role != host_peers.PRIMARY or not self.ready \
                or not host_peers.keyed(params, self.peer_key):
            return
        record = params.get("record")
        if not isinstance(record, dict):
            return
        name, arguments = record.get("name"), record.get("arguments")
        scope, items = record.get("scope"), record.get("items")
        if name not in RETURNING_TOOLS or name not in self.tool_names \
                or not isinstance(arguments, dict) or not isinstance(scope, str) \
                or not isinstance(items, list):
            return
        self.agent_run.record(name, error=False, host_code=False)
        self._unkept = None
        try:
            host = self._ontology_host()
            gated = self.case_runtime.employee_gate(self.actor)
            reads = self._item_reads(host)
            with host.request():
                now = _evidence_scope(host.store_meta(), host.policy_for(gated))
            if now != scope:
                return   # read under another generation or grant: not evidence of now
            theirs = {tuple(key): digest for key, digest in (
                entry for entry in items if isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[0], list) and isinstance(entry[1], str))}
            self._read_scope = (now, reads)
            self._note_returned(name, arguments, record.get("built"), record.get("shown"))
            changed = {key for key, digest in theirs.items()
                       if key in self._items and self._items[key] != digest}
            if changed or record.get("kept") is not True:
                _drop_changed(self._evidence, changed or None)
        except Exception:  # noqa: BLE001 - undecided: nothing an item gates is kept
            _drop_changed(self._evidence, None)
        finally:
            self._read_scope = None
            self._unkept = None
            self._close_reads()

    @staticmethod
    def _decider(host: Any, gated: Any) -> tuple[Any, Callable[[Any], dict[str, Any]]]:
        """This call's policy and its access decision per citation handle (``HostAccess.
        _decide``: the record's audience unit, then the item permission of a document or
        mail; ``None``: readable), each handle decided once per call."""
        from operator_api.workspace.object_workspace import HostAccess

        policy = host.policy_for(gated)
        decided: dict[str, Any] = {}

        def decide(handles: Any) -> dict[str, Any]:
            handles = list(dict.fromkeys(handles))
            todo = [handle for handle in handles if handle not in decided]
            if todo:
                with host.per_call():
                    gate = host.tools.item_gate
                    for handle in todo:
                        decided[handle] = HostAccess._decide(handle, gated, policy, gate)
            return {handle: decided[handle] for handle in handles}

        return policy, decide

    def _home_mode(self, host: Any, gated: Any, arguments: dict[str, Any],
                   found: Any) -> Any:
        """plx.find's home mode: a query that is one of the home phrases (``home_kinds``)
        and matched no record answers with the reader's gap inbox (``_inbox_answer``). A
        record found by the words wins (conservative identity); a filter or object_type
        is never home."""
        kinds = home_kinds(arguments.get("query"))
        # the late list may be asked with its own type named (object_type PurchaseOrder)
        typed = (set(kinds or ()) <= set(LATE_PO_KINDS) and bool(kinds)
                 and arguments.get("object_type") == "PurchaseOrder")
        if (kinds is None or not _no_record_named(found)
                or set(arguments) - {"query", "limit", "response_format"}
                - ({"object_type"} if typed else set())):
            return found
        return self._inbox_answer(host, gated, kinds, arguments, found)

    @staticmethod
    def _scope_required(found: dict[str, Any], plan: dict[str, Any], root_type: str,
                        refused: list[str]) -> dict[str, Any]:
        """A portfolio question not scanned: the gap names the scopes that start a fresh
        scan and why the given filters are none; never complete."""
        kinds = mcp_portfolio.SCOPE_PROPERTIES.get(root_type) or {}
        allowed = "; ".join(f"{kind.replace('_', ' ')}: {', '.join(props)}"
                            for kind, props in kinds.items())
        gap = ontology_tools._gap(
            "unknown", "portfolio_scope_required", f"{plan['id']} across {root_type}",
            "a question across records is scanned only within a scope: filters on "
            f"{root_type} naming a supplier, an order set or a date range of at most "
            f"{mcp_portfolio.SCOPE_MAX_DAYS} days (ET) ({allowed or 'none declared'}); "
            f"not a scope: {', '.join(refused[:8])}")
        return {**found, "gaps": [gap, *(found.get("gaps") or [])][:20],
                "completeness": {"complete": False, "truncated": False, "returned": 0,
                                 "total": None, "reason": "portfolio_scope_required"}}
