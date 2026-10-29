"""Read-only case workspace for the operator console.

One host-configured actor reads durable cases through the same public functions the
MCP case tools use (``CaseStore.get``/``list``, ``CaseRuntime.resolve_citation``,
``citation_status`` and ``read_original``); every read carries that actor, so the
case audience, the source-mirror relation audience and the per-original audience
apply exactly as they do for ``plx.case_get``/``plx.source_citation``. A case the
actor may not read, a citation it cannot resolve and an original outside its
audience all read as "not found" (no oracle).

Nothing here writes: no case event, no proposal approval or dispatch, no business
system. Proposals are shown as records that need a person's approval in Mission
Control, with their goal-2 action-path state when the host configured the action
store (``operating.action_path``, read for the same actor).

The workspace shows, for one case and one reader, the timeline, the originals (with
their re-hashed SHA-256 and, for SharePoint originals, the eTag status of the source
item read now), the conflicts recorded on the case (``conflicts``), the proposals with
their approval state, the mail and BC records the case cites and the ingestion coverage
of ``case_context_tools.ingest_status`` (when the host configured ingest sources). It
draws no relationship map and decides no mail link of its own: relationships, their
identity tiers (a mail message's stored mention links to a purchase order included) and
a person's link reviews are the ontology's (the object workspace, ``plx.around`` and
``plx.review``), and ``operating.case_mail`` records a case's mail as cited findings,
never a second join here.
``evidence`` is the host's statement of what the state directory holds (e.g.
fixture); the UI shows it on every case.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ontology.core import Actor, ValidationError
from operating.case_runtime import LIMITATIONS, CaseRuntime
from operating.cases import CITATION_CONTENT_CHANGED, CaseNotFound, CitationUnresolvable
from operator_ui.case_plain import (case_summary, plain_gap, question_root_types,
                                   source_date_label)

MAX_HANDLE = 2048
MAX_RECORDS = 400
MAX_ORIGINALS = 50
MAX_PAGE_LINKS = 50
SECTION_LIMIT = 200
CASE_SECTIONS = ("findings", "calculations", "gaps", "proposals", "verifications", "timeline",
                 "records", "source_dates")
MAX_TEXT_CHARS = 20_000
MAX_SOURCE_DATES = 600
MAX_LIVE_NODES = 50
EVIDENCE_UNSTATED = {
    "label": "recorded case state",
    "fixture": False,
    "note": ("The host did not state where this state directory's data came from. Each "
             "citation is re-read for you; the evidence label of the run that wrote the "
             "case is not recorded here.")}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]{0,40})?\Z")
_ORIGINAL_BASE = re.compile(r"(original://[^#\s]+)")
APPROVAL_NOTE = ("Recorded only. Needs human approval in Mission Control; the Workbench "
                 "never approves, dispatches or executes it.")
_ITEM_SECTIONS = ("gaps", "proposals", "verifications")
# Action-path states after a dispatch was recorded as sent; dispatched_unknown and
# escalated may or may not have written, so a withheld reader is told "unknown".
_SENT_STATES = frozenset({"dispatched", "verified"})
_UNCERTAIN_STATES = frozenset({"dispatched_unknown", "escalated"})


def _dispatched_by_state(state: Any) -> bool | None:
    if state in _SENT_STATES:
        return True
    return None if state in _UNCERTAIN_STATES else False


class CaseWorkspace:
    """The console's read-only window onto a ``CaseRuntime`` for one actor.

    ``report_store`` is an optional ``operating.playbook_reports.PlaybookReportStore``;
    its own ``can_read`` still applies, and it is read only after the case itself
    was read for the actor.
    """

    def __init__(self, runtime: CaseRuntime, actor: Actor, *, report_store: Any = None,
                 evidence: Mapping[str, Any] | None = None, action_path: Any = None,
                 ingestion: Any = None) -> None:
        """``action_path``: an ``operating.action_path.ActionPath`` over this runtime's
        case store (read only here); ``ingestion``: an ``IngestionStatusSource``."""
        if not isinstance(actor, Actor):
            raise ValueError("case_workspace_requires_actor")
        if runtime is None:
            raise ValueError("case_workspace_requires_case_runtime")
        self.runtime = runtime
        self.actor = actor
        self.report_store = report_store
        self.evidence = dict(evidence) if evidence else dict(EVIDENCE_UNSTATED)
        self.action_path = action_path
        self.ingestion = ingestion

    # ---- list ------------------------------------------------------------

    def list_cases(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        try:
            cases = self.runtime.cases.list(self.actor, status=status, limit=limit)
        except ValidationError as error:
            raise ValueError(str(error)) from None
        return {"available": True, "cases": cases, "limitations": list(LIMITATIONS),
                "evidence": dict(self.evidence), "business_system_writes": 0}

    # ---- one case --------------------------------------------------------

    def case(self, case_id: str, *, section: str | None = None, offset: int = 0,
             limit: int | None = None) -> dict[str, Any]:
        """The case workspace for the actor.

        Without ``section`` every list is capped at ``SECTION_LIMIT`` items (findings: in
        total) and ``truncated`` names each capped list with its total; ``section`` pages
        one list (findings, calculations, gaps, proposals, verifications, timeline,
        records, source_dates) with ``offset``/``limit`` until ``next_offset`` is null.
        """
        if limit is None:
            limit = SECTION_LIMIT
        if section is not None and section not in CASE_SECTIONS:
            raise ValueError("invalid_case_section")
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= SECTION_LIMIT):
            raise ValueError("invalid_case_page")
        try:
            view = self.runtime.cases.get(self.actor, case_id)
        except CaseNotFound:
            raise KeyError("case_not_found") from None
        statuses: dict[str, dict[str, Any]] = {}

        def status_of(handle: str) -> dict[str, Any]:
            if handle not in statuses:
                statuses[handle] = self.runtime.citation_status(handle, self.actor)
            return statuses[handle]

        cited: dict[str, list[dict[str, Any]]] = {}

        def annotate(item: dict[str, Any], kind: str, cites: list[dict[str, Any]]) -> None:
            for cite in cites:
                handle = cite.get("handle")
                if handle and cite.get("status") in ("resolved", CITATION_CONTENT_CHANGED):
                    cite["mirror"] = status_of(handle)
                    if cite["status"] == "resolved":
                        cited.setdefault(handle, []).append(
                            {"kind": kind, "id": item.get("id")})

        for bucket, items in view["findings"].items():
            for item in items:
                annotate(item, "finding", item.get("citations") or [])
        for calc in view["calculations"]:
            annotate(calc, "calculation", [spec["citation"] for spec in
                                           (calc.get("inputs") or {}).values()
                                           if spec.get("kind") == "citation"])
        for name in _ITEM_SECTIONS:
            for item in view[name]:
                annotate(item, name[:-1], item.get("citations") or [])
        verifications: dict[str, list[dict[str, Any]]] = {}
        for item in view["verifications"]:
            verifications.setdefault(item["proposal_id"], []).append(item)
        intents = self._intents(view["case_id"])
        for proposal in view["proposals"]:
            proposal["approval"] = self._approval(proposal, intents)
            proposal["verifications"] = verifications.get(proposal["id"], [])
        records, source_dates, rows_cited, rows = self._records(cited)
        whole = {**view, "records": records, "source_dates": source_dates}
        # Presentation only: employee wording beside each recorded gap (kept as recorded)
        # and the business summary, both from what this reader resolved above.
        summary = case_summary(view, rows)
        root_types = [root["type"] for root in summary["roots"] if root.get("type")] \
            or question_root_types(view.get("question"))
        for gap in view["gaps"]:
            if not gap.get("content"):
                gap["plain"] = plain_gap(gap, root_types)
        findings = sorted(({**item, "bucket": bucket}
                           for bucket, items in view["findings"].items() for item in items),
                          key=lambda item: item["seq"])
        lists = {name: whole[name] for name in CASE_SECTIONS if name != "findings"}
        lists["findings"] = findings
        base = {"case_id": view["case_id"], "status": view["status"],
                "integrity": view["integrity"], "limitations": list(LIMITATIONS),
                "evidence": dict(self.evidence), "read_only": True,
                "business_system_writes": 0}
        if section is not None:
            items = lists[section]
            end = min(len(items), offset + limit)
            return {**base, "view": "section", "section": section, "offset": offset,
                    "limit": limit, "total": len(items), "items": items[offset:end],
                    "next_offset": end if end < len(items) else None}
        # The evaluation is judged on the reader's whole (uncapped) view.
        evaluation = self.evaluation(view["case_id"], whole)
        truncated: dict[str, dict[str, int]] = {}
        for name, items in lists.items():
            if len(items) > SECTION_LIMIT:
                truncated[name] = {"shown": SECTION_LIMIT, "total": len(items)}
        if "findings" in truncated:
            kept = {item["id"] for item in findings[:SECTION_LIMIT]}
            whole["findings"] = {bucket: [item for item in items if item["id"] in kept]
                                 for bucket, items in view["findings"].items()}
        for name in lists:
            if name != "findings" and name in truncated:
                # Timeline: the newest events; every other list: the first in case order.
                whole[name] = (lists[name][-SECTION_LIMIT:] if name == "timeline"
                               else lists[name][:SECTION_LIMIT])
        if rows_cited > MAX_RECORDS:
            truncated["records_cited"] = {"shown": MAX_RECORDS, "total": rows_cited}
        originals = self._originals(cited)
        live = self._live(cited)
        for original in originals:
            at = (original.get("source") or {}).get("last_modified")
            if isinstance(at, str) and _DATE.fullmatch(at.strip()):
                whole["source_dates"].append({
                    "at": at.strip(), "column": "last modified (SharePoint item)",
                    "relation": "original", "key": original.get("filename"),
                    "handle": original["citation"]})
        for item in live:
            if isinstance(item.get("at"), str) and _DATE.fullmatch(item["at"].strip()):
                whole["source_dates"].append({
                    "at": item["at"].strip(), "column": item.get("date_column") or "date",
                    "relation": item["kind"], "key": item.get("label"),
                    "handle": item["handle"]})
        listed = conflicts(view)
        plain = {gap.get("id"): gap["plain"] for gap in view["gaps"] if "plain" in gap}
        for item in listed:
            if item["kind"] == "evidence_conflict" and item["id"] in plain:
                item["plain"] = plain[item["id"]]
        return {**whole, **base, "view": "workspace", "summary": summary,
                "originals": originals, "evaluation": evaluation,
                "conflicts": listed,
                "source_coverage": self.source_coverage(),
                "truncated": truncated, "paging": (
                    "GET /api/case/<id>?section=<name>&offset=<n>&limit=<1..200> pages a "
                    "truncated list until next_offset is null") if truncated else None,
                "actor": {"tenant": self.actor.tenant,
                          "principals": sorted(self.actor.principals)}}

    def _records(self, cited: Mapping[str, list[dict[str, Any]]]
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int,
                            dict[str, list[dict[str, Any]]]]:
        """Cited source rows (resolved for the actor now), their ISO date columns and the
        resolved rows by handle (in memory, for ``case_summary``; never sent as is)."""
        records, dates, resolved = [], [], {}
        rows_cited = sorted(handle for handle in cited if handle.startswith("api://"))
        for handle in rows_cited[:MAX_RECORDS]:
            try:
                view = self.runtime.resolve_citation(handle, self.actor)
            except CitationUnresolvable:
                continue  # resolved a moment ago; now gone: show nothing about it
            record = {"handle": handle, "relation": view["relation"], "key": view["key"],
                      "cited_by": cited[handle]}
            rows = [view["row"]] if "row" in view else list(view.get("rows") or [])
            resolved[handle] = rows
            if len(rows) != 1:
                record["key_unique"] = False
            for row in rows:
                for column, value in row.items():
                    if (isinstance(value, str) and _DATE.fullmatch(value.strip())
                            and len(dates) < MAX_SOURCE_DATES):
                        dates.append({"at": value.strip(), "column": column,
                                      "label": source_date_label(view["relation"], column),
                                      "relation": view["relation"], "key": view["key"],
                                      "handle": handle})
            records.append(record)
        return records, dates, len(rows_cited), resolved

    def _originals(self, cited: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Originals cited on the case and visible to the actor, with their page links."""
        bases: dict[str, list[str]] = {}
        for handle in cited:
            match = _ORIGINAL_BASE.match(handle)
            if match:
                bases.setdefault(match.group(1), []).append(handle)
        out = []
        for base in sorted(bases)[:MAX_ORIGINALS]:
            view = self.runtime.read_original(base, self.actor)
            if not view.get("ok"):
                continue
            extraction = view.get("extraction") or {}
            ocr_pages = list(extraction.get("ocr_pages") or [])
            pages = []
            for page_handle in list(view.get("page_citations") or [])[:MAX_PAGE_LINKS]:
                number = re.search(r"page=(\d+)", page_handle)
                page = int(number.group(1)) if number else None
                pages.append({"citation": page_handle, "page": page,
                              "text_source": "ocr" if page in ocr_pages else "text_layer"})
            out.append({"citation": base, "original_id": view.get("original_id"),
                        "filename": view.get("filename"), "sha256": view.get("sha256"),
                        "bytes": view.get("bytes"), "media_type": view.get("media_type"),
                        "extraction": {key: extraction.get(key) for key in (
                            "status", "method", "error", "page_count", "ocr_pages")},
                        "latest_extraction_version": view.get("latest_extraction_version"),
                        "pages": pages, "pages_total": len(view.get("page_citations") or []),
                        "pages_truncated": len(
                            view.get("page_citations") or []) > MAX_PAGE_LINKS,
                        "cited_as": sorted(bases[base]),
                        "cited_by": [ref for handle in sorted(bases[base])
                                     for ref in cited[handle]],
                        "evidence_mode": view.get("evidence_mode"),
                        "integrity": {"sha256_rehashed_on_this_read": True,
                                      "status": view.get("status")},
                        "source": self._original_source(view.get("provenance"))})
        return out

    def _original_source(self, provenance: Any) -> dict[str, Any]:
        """Where an original came from and, for a SharePoint item, its eTag now.

        The source item is re-read for the actor through the host's read-only reader
        (``citation_status`` of the item's ``sharepoint://`` citation: eTag-based, no
        download); without a reader the eTag is not rechecked and says so.
        """
        if not isinstance(provenance, Mapping):
            return {"kind": "operator-inbox",
                    "note": "attached from the operator originals inbox; no source item "
                            "to recheck"}
        source = {"kind": provenance.get("source"), "path": provenance.get("path"),
                  "name": provenance.get("name"), "etag_at_attach": provenance.get("etag"),
                  "last_modified": provenance.get("last_modified"),
                  "read_at": provenance.get("read_at"),
                  "original_url": provenance.get("original_url"),
                  "citation": provenance.get("source_citation")}
        handle = provenance.get("source_citation")
        if not isinstance(handle, str):
            return {**source, "etag_status": "not_rechecked",
                    "etag_reason": "no source citation recorded"}
        status = self.runtime.citation_status(handle, self.actor)
        if status.get("ok") and status.get("status") == "current":
            source.update(etag_status="current", etag_reason=(
                "the item's eTag read now equals the eTag at attach"))
        elif status.get("ok"):
            source.update(etag_status="changed_since", etag_reason=(
                "the item changed in SharePoint after it was attached; the stored bytes "
                "are the attached version"))
        else:
            error = str(status.get("error") or "unavailable")
            source.update(etag_status=("not_rechecked" if error.startswith(
                "source_not_configured") else "unavailable"), etag_reason=error[:120])
        return source

    def _live(self, cited: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Mail and BC citations on the case that resolve for the actor now."""
        out = []
        handles = sorted(h for h in cited if h.startswith(("graph-mail://", "bc://")))
        for handle in handles[:MAX_LIVE_NODES]:
            try:
                view = self.runtime.resolve_citation(handle, self.actor)
            except CitationUnresolvable:
                continue
            record = view.get("record") or {}
            if handle.startswith("graph-mail://"):
                label = str(record.get("subject") or "mail message")[:120]
                out.append({"handle": handle, "kind": "mail", "label": label,
                            "at": record.get("received_at"), "date_column": "received",
                            "from": record.get("from")})
            else:
                label = str(record.get("number") or record.get("displayName")
                            or record.get("id") or "BC record")[:120]
                out.append({"handle": handle, "kind": "bc", "label": label,
                            "at": record.get("lastModifiedDateTime"),
                            "date_column": "last modified (BC)"})
        return out

    def source_coverage(self) -> dict[str, Any]:
        """Counts-only ingestion status for the actor (``case_context_tools.ingest_status``)."""
        if self.ingestion is None:
            return {"status": "not_configured"}
        from operating import case_context_tools
        from operating.employee_permissions import EmployeeAccessRefused

        try:
            status = case_context_tools.ingest_status(self.runtime, self.actor,
                                                      self.ingestion)
        except (EmployeeAccessRefused, PermissionError) as error:
            return {"status": "refused", "error": type(error).__name__}
        return {"status": "read", **status}

    def _intents(self, case_id: str) -> dict[str, dict[str, Any]]:
        """Goal-2 action intents of this case as the actor may see them, by proposal id.

        ``ActionPath.list_for_case`` reads the case for the actor first and returns each
        intent through ``public_view``: an intent whose proposal is withheld from the
        actor carries its state only. An intent whose event chain does not verify has no
        readable proposal id (``_chain_broken``), and a listing over the store's bound
        says so (``_truncated``): either makes an unmatched proposal's state unknown,
        never "not registered".
        """
        path = self.action_path
        if path is None:
            return {}
        import sqlite3

        try:
            listing = path.list_for_case(self.actor, case_id)
        except CaseNotFound:
            return {"_error": {"error": "case_not_readable_for_action_listing"}}
        except (ValidationError, sqlite3.Error) as error:
            # ActionPathError is a ValidationError; the code names the failure.
            return {"_error": {"error": str(getattr(error, "code", None) or error)[:120]}}
        out: dict[str, dict[str, Any]] = {}
        broken = 0
        for item in listing.get("intents") or []:
            if item.get("integrity") == "action_event_chain_broken" or not item.get(
                    "proposal_id"):
                broken += 1
                continue
            out.setdefault(item["proposal_id"], item)  # first registration wins
        if broken:
            out["_chain_broken"] = {"intents": broken}
        if listing.get("truncated"):
            out["_truncated"] = {"listed": len(listing.get("intents") or []),
                                 "total": listing.get("total")}
        return out

    @staticmethod
    def _approval(proposal: Mapping[str, Any], intents: Mapping[str, Any]) -> dict[str, Any]:
        """A proposal's approval state: never approved or dispatched by this console."""
        base = {"where": "Mission Control", "note": APPROVAL_NOTE, "approved_here": False}
        if "_error" in intents:
            return {**base, "state": "action_store_unreadable", "dispatched": None,
                    "detail": intents["_error"].get("error")}
        state = intents.get(proposal.get("id"))
        if state is None and "_chain_broken" in intents:
            # A broken intent's proposal cannot be read back: it may be this one.
            return {**base, "state": "unknown_chain_broken", "dispatched": None,
                    "detail": (f"{intents['_chain_broken']['intents']} action record(s) of "
                               "this case fail their hash chain; which proposal they "
                               "belong to cannot be read")}
        if state is None and "_truncated" in intents:
            # Registrations past the store's listing bound were not read: unknown.
            listed = intents["_truncated"]
            return {**base, "state": "unknown_list_truncated", "dispatched": None,
                    "detail": (f"only the first {listed['listed']} of {listed['total']} "
                               "action registrations of this case were listed")}
        if state is None:
            return {**base, "state": "not_registered", "dispatched": False,
                    "path": "not on the goal-2 action path: no Mission Control approval "
                            "has been requested and nothing can be dispatched"}
        if state.get("withheld") or proposal.get("content"):
            # The proposal is withheld from this reader: its intent shows its state only
            # (the action path withholds the capability, preconditions and the intent id,
            # a digest of the content).
            return {**base, "state": state["state"], "registered": True,
                    "dispatched": _dispatched_by_state(state["state"]), "withheld": True}
        dispatched = any(item.get("outcome") == "dispatched"
                         for item in state.get("dispatches") or [])
        recovery = state.get("recovery") or []
        safe = state.get("next_safe_step") or {}
        return {**base, "state": state["state"], "registered": True,
                "intent_id": state.get("intent_id"), "capability": state.get("capability"),
                "owner_system": state.get("owner_system"), "dispatched": dispatched,
                "dispatch_attempts": len(state.get("dispatches") or []),
                "gate": ({key: state["gate"].get(key) for key in ("task_id", "gate_id")}
                         if state.get("gate") else None),
                "decisions": [{key: item.get(key) for key in ("status", "authorizes",
                                                                "reason_code", "decided_at")}
                              for item in state.get("decisions") or []],
                "read_backs": [item.get("outcome") for item in state.get("read_backs") or []],
                "human_preconditions": list(state.get("human_preconditions") or []),
                "checked_preconditions": len(state.get("checked_preconditions") or []),
                "next_step": (recovery[-1] if recovery else {}).get("next_step"),
                "next_safe_step": {key: safe.get(key) for key in ("step", "by", "detail")}
                                  if safe else None,
                "history": [{key: item.get(key) for key in ("kind", "at")}
                            for item in state.get("history") or []],
                "chain_ok": state.get("chain_ok")}

    @staticmethod
    def _view_complete(view: Mapping[str, Any]) -> bool:
        """True when nothing on the case is withheld from or changed for this reader.

        A stored evaluation was computed by someone who could read every citation; a
        reader who cannot (or who sees changed content) would learn from its counts,
        check ids, limits and recorder what the citation gate hides from them.
        """
        findings = view["findings"]
        if findings.get("withheld") or findings.get(CITATION_CONTENT_CHANGED):
            return False
        # Every other bucket too: a superseded finding keeps its citations, and one
        # whose cited content changed (or no longer resolves) is not the case the
        # evaluator read either.
        for items in findings.values():
            for item in items:
                if item.get("content") is not None or any(
                        cite.get("status") != "resolved"
                        for cite in item.get("citations") or []):
                    return False
        for name in ("calculations", "gaps", "proposals", "verifications", "links"):
            for item in view.get(name) or []:
                if item.get("content") is not None:
                    return False
                if CITATION_CONTENT_CHANGED in (item.get("inputs_status"),
                                                item.get("citations_status")):
                    return False
        return True

    @staticmethod
    def _freshness(boundary: Any, view: Mapping[str, Any]) -> dict[str, Any]:
        """Is the stored evaluation about the case as it is now?

        A report pins the case event boundary it read (``playbook_eval._case_boundary``:
        event count and last event). The case is append-only, so any later event (a
        finding, gap, proposal, verification, close) makes the report ``stale``: it
        scored an earlier case. A report without a boundary is ``unknown``, never
        current.
        """
        if not isinstance(boundary, Mapping) or not isinstance(boundary.get("events"), int):
            return {"freshness": "unknown", "freshness_reason": (
                "the stored report records no case event boundary")}
        events = (view.get("integrity") or {}).get("events")
        timeline = view.get("timeline") or []
        last = timeline[-1] if timeline else {}
        evaluated_last = boundary.get("last_event") or {}
        current = {"evaluated_events": boundary["events"], "current_events": events}
        if events != boundary["events"] or (
                isinstance(evaluated_last, Mapping) and evaluated_last.get("seq") is not None
                and any(evaluated_last.get(key) != last.get(key)
                        for key in ("seq", "kind", "at"))):
            return {"freshness": "stale", **current, "freshness_reason": (
                f"evaluated at {boundary['events']} case events; the case now has "
                f"{events}: it changed after this evaluation")}
        return {"freshness": "current", **current}

    def evaluation(self, case_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
        """Latest stored playbook evaluation summary for this (already readable) case.

        Withheld, with nothing about it, unless the reader's view of the case is complete.
        The one evaluator: the case view and the object view's case panel both call it.
        """
        store = self.report_store
        if store is None:
            return {"status": "not_configured"}
        if not self._view_complete(view):
            return {"status": "withheld", "reason": (
                "part of this case is withheld from you or changed since cited, so a "
                "stored evaluation computed over the whole case is not shown")}
        try:
            reports = store.list(self.actor, case_id=case_id)
            if not reports:
                return {"status": "none"}
            stored = store.get(self.actor, reports[0]["id"])
        except LookupError:
            return {"status": "none"}
        except Exception as error:  # integrity failures are shown, never hidden
            return {"status": "integrity_error", "error": type(error).__name__,
                    "detail": str(error)[:200]}
        report = stored["report"]
        summary = report.get("summary") or {}
        freshness = self._freshness(report.get("case_boundary"), view)
        return {"status": "stored", **freshness, "report_id": stored["id"],
                "report_sha256": stored["report_sha256"],
                "playbook_id": stored["playbook_id"],
                "playbook_version": stored["playbook_version"],
                "evaluator_version": stored["evaluator_version"],
                "recorded_at": stored["recorded_at"],
                "transcript_source": stored["transcript_source"],
                "passed_all": stored["passed_all"],
                "passed_all_clean": stored["passed_all_clean"],
                "passed_all_qualification": summary.get("passed_all_qualification"),
                "authority_checked": summary.get("authority_checked"),
                "limits": list(report.get("limits") or []),
                "checks": summary.get("checks"), "passed": summary.get("passed"),
                "failed": summary.get("failed"),
                "failed_ids": list(summary.get("failed_ids") or []),
                "gaps_missing": summary.get("gaps_missing"),
                "gaps_unknown": summary.get("gaps_unknown"),
                "reports_for_case": len(reports)}

    # ---- original bytes ----------------------------------------------------

    def original_pdf(self, handle: str) -> dict[str, Any]:
        """The PDF bytes of one original for the actor (``OriginalStore.read_pdf``).

        The store checks the audience (and source gate, and employee principal)
        before it reads a byte; a refused or missing original is "not found".
        """
        if (not isinstance(handle, str) or not handle or len(handle) > MAX_HANDLE
                or not handle.startswith("original://") or "#" in handle):
            raise ValueError("original_file_citation_required")
        try:
            return self.runtime.originals.read_pdf(handle, self.actor)
        except CitationUnresolvable as error:
            if str(error) == "original_file_citation_required":
                raise ValueError(str(error)) from None
            raise KeyError("citation_unresolvable_for_actor") from None

    # ---- document metadata proposals --------------------------------------

    def document_metadata(self, handle: str) -> dict[str, Any]:
        """``plx.document_fields`` mode ``propose`` for one original the actor can read: a
        proposal, never a fact. A person confirms it through that tool's form; the
        confirmation reaches the ontology's SharePointFile object
        (``operating.kind_confirmation.projection_step``), not a separate tag store."""
        if (not isinstance(handle, str) or not handle or len(handle) > MAX_HANDLE
                or not handle.startswith("original://")):
            raise ValueError("original_citation_required")
        from operating import case_context_tools

        try:
            result = case_context_tools.document_metadata(self.runtime, self.actor, handle)
        except CitationUnresolvable:
            raise KeyError("citation_unresolvable_for_actor") from None
        return {**result, "proposals_only": True, "read_only": True,
                "business_system_writes": 0}

    # ---- one citation ----------------------------------------------------

    def citation(self, handle: str, *, text_offset: int = 0,
                 max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
        """One citation as the actor sees it now (``plx.source_citation`` semantics)."""
        if (not isinstance(handle, str) or not handle or len(handle) > MAX_HANDLE
                or not handle.startswith(("api://", "original://"))):
            raise ValueError("citation_scheme_not_supported")
        if handle.startswith("original://"):
            view = self.runtime.read_original(handle, self.actor)
            if not view.get("ok"):
                raise KeyError("citation_unresolvable_for_actor")
            if "text" in view:
                text = view.pop("text")
                if text_offset > len(text):
                    raise ValueError("text_offset_out_of_bounds")
                end = min(len(text), text_offset + max_chars)
                view.update(text=text[text_offset:end], text_offset=text_offset,
                            text_chars=len(text), next_text_offset=end if end < len(text)
                            else None)
            pages = view.get("page_citations")
            if isinstance(pages, list) and len(pages) > MAX_PAGE_LINKS:
                view.update(page_citations=pages[:MAX_PAGE_LINKS],
                            page_citations_truncated={"shown": MAX_PAGE_LINKS,
                                                      "total": len(pages)})
            return {**view, "kind": "original", "read_only": True}
        status = self.runtime.citation_status(handle, self.actor)
        if not status.get("ok"):
            raise KeyError("citation_unresolvable_for_actor")
        try:
            view = self.runtime.resolve_citation(handle, self.actor)
        except CitationUnresolvable:
            raise KeyError("citation_unresolvable_for_actor") from None
        return {**view, "kind": "row", "status": status, "read_only": True}




def conflicts(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every disagreement recorded on the case that this reader can see, with its
    citations by side: gaps recorded as an evidence conflict (a display rule over the
    gap's recorded question: the case store has no structured gap kind), contradicted
    findings and links an investigator recorded as contradicted (``plx.case_add_finding``
    link mode). A withheld item carries no citation and is not listed."""
    out: list[dict[str, Any]] = []
    for gap in view.get("gaps") or []:
        missing = str(gap.get("missing") or "").lower()
        if gap.get("content") or "evidence conflict" not in missing:
            continue
        handles = _resolved(gap.get("citations"))
        paper = [h for h in handles if h.startswith("original://")]
        source = [h for h in handles if not h.startswith("original://")]
        sides = []
        if source:
            sides.append({"side": "source records", "citations": source})
        if paper:
            sides.append({"side": "paper original (machine-read; a person confirms)",
                          "citations": paper})
        out.append({"kind": "evidence_conflict", "id": gap.get("id"),
                    "question": gap.get("missing"), "why": gap.get("why"),
                    "state": gap.get("state"), "who_can_answer": gap.get("who_can_answer"),
                    "sides": sides, "basis": "gap recorded as an evidence conflict"})
    for item in (view.get("findings") or {}).get("contradicted") or []:
        out.append({"kind": "contradicted_finding", "id": item.get("id"),
                    "question": item.get("statement"),
                    "sides": [{"side": "cited sources", "citations": [
                        c["handle"] for c in item.get("citations") or []
                        if isinstance(c, Mapping) and isinstance(c.get("handle"), str)]}],
                    "basis": "finding recorded as contradicted by its cited sources"})
    for link in view.get("links") or []:
        handles = _resolved(link.get("citations"))
        if link.get("content") or link.get("outcome") != "contradicted" or len(handles) != 2:
            continue
        contract = (link.get("contract") or {}).get("id")
        out.append({"kind": "link_contradicted", "id": contract,
                    "question": f"link contract {contract}: contradicted (recorded on the "
                                f"case as {link.get('id')})",
                    "sides": [{"side": "the two cited records", "citations": handles}],
                    "basis": "the two cited rows disagree on the stored value a declared "
                             "link contract names"})
    return out


def _resolved(citations: Sequence[Any] | None) -> list[str]:
    return [c["handle"] for c in citations or []
            if isinstance(c, Mapping) and c.get("status") == "resolved"]


__all__ = ["APPROVAL_NOTE", "CaseWorkspace", "conflicts"]
