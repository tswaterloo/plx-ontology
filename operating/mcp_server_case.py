"""The case host's case-runtime calls: links, claims and attached originals."""

from __future__ import annotations

from typing import Any

from . import capability_guards, case_context_tools, ontology_tools, session_identity
from .case_runtime import CaseRuntime, LIVE_SCHEMES
from .case_originals import OriginalError, SCHEME as SCHEME_ORIGINAL

from .mcp_server_results import (
    _arg, _label_claim, _model_suggestions, _pages_read, _TO_CLIENT, InputError
)


class CaseMixin:
    """Case-runtime calls: links, claims and attached originals."""

    @staticmethod
    def _staleness(arguments: dict[str, Any]) -> float | None:
        value = arguments.get("max_staleness")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError("invalid_argument_type:max_staleness")
        return value

    @staticmethod
    def _case_id(arguments: dict[str, Any]) -> str:
        return _arg(arguments, "case_id", str, required=True, lower=1, upper=64)

    @staticmethod
    def _citation_handles(values: list[Any]) -> list[str]:
        for value in values:
            if not isinstance(value, str):
                raise InputError("invalid_argument_type:citations")
            if not value.strip().startswith(("api://", "original://") + LIVE_SCHEMES):
                raise InputError("citation_scheme_not_supported: only api:// citations "
                                 "(plx.object value citations), original:// "
                                 "citations from plx.case_attach_original and graph-mail://, "
                                 "sharepoint://, bc:// citations from the live sources resolve")
        return values

    def _link(self, runtime: CaseRuntime, actor: Any,
              arguments: dict[str, Any]) -> dict[str, Any]:
        """``plx.case_add_finding`` link mode: a Workbench-checked link between two cited
        rows under a declared link contract (``CaseStore.link``); the Workbench writes
        the finding, so nothing else of a finding may be passed."""
        if set(arguments) - {"case_id", "link"}:
            raise InputError("link_mode_takes_only:case_id,link")
        case_id = self._case_id(arguments)
        link = _arg(arguments, "link", dict, required=True)
        if set(link) - {"from_citation", "to_citation", "contract_id"}:
            raise InputError("unknown_arguments:link")
        handles = self._citation_handles([
            _arg(link, "from_citation", str, required=True, lower=8, upper=2048),
            _arg(link, "to_citation", str, required=True, lower=8, upper=2048)])
        contract_id = _arg(link, "contract_id", str, required=True, lower=1, upper=80)
        if contract_id not in runtime.link_contracts.ids():
            # The contracts that fit the cited rows' relations first: the whole list no
            # longer fits one error message (it is cut at 400 characters).
            cited = [h[len("api://"):].split("/", 1)[0] if h.startswith("api://") else None
                     for h in handles]
            contracts = runtime.link_contracts
            fits = lambda c: (contracts.get(c)["from"], contracts.get(c)["to"])  # noqa: E731
            ordered = sorted(contracts.ids(), key=lambda c: (
                -sum(a == b for a, b in zip(fits(c), cited)), c))
            raise InputError("unknown_link_contract: one of " + ", ".join(ordered))
        recorded = runtime.link(actor, case_id, handles[0], handles[1], contract_id)
        result = {"link": recorded, "business_system_writes": 0}
        if not recorded["recorded"]:
            result["next_tools"] = ["plx.case_record_gap"]
        return result

    def _document_fields_proposal(self, runtime: CaseRuntime, actor: Any,
                                  arguments: dict[str, Any]) -> dict[str, Any]:
        """``plx.document_fields`` mode propose: Portal-taxonomy metadata proposals for one
        attached original the actor can read (proposals only; nothing is saved)."""
        stray = set(arguments) & {"columns", "proposed_document_kind",
                                  "expected_proposal_digest"}
        if stray:
            raise InputError(f"{sorted(stray)[0]}_applies_to_mode_request")
        handle = _arg(arguments, "citation", str, required=True, lower=8, upper=200)
        if "page=" in handle:
            raise InputError("original_file_citation_required: pass original://<id> "
                             "(or #extraction=<k>), not a page citation")
        if not handle.startswith("original://"):
            raise InputError("citation_scheme_not_supported: an original:// file "
                             "citation from plx.case_attach_original is required")
        suggestions = _model_suggestions(
            _arg(arguments, "model_suggestions", list, default=[], upper=20))
        return case_context_tools.document_metadata(
            runtime, actor, handle, model_suggestions=suggestions,
            max_staleness=self._staleness(arguments))

    def _ontology_schema(self) -> Any:
        """The compiled ontology schema this host serves through plx.schema."""
        if self.ontology is not None:
            return self.ontology.schema
        return ontology_tools.shared_schema()

    def _author_kind(self) -> str:
        """Who writes this session's case text: its session's author kind; an MCP
        session's text is an agent's (``session_identity.CHANNEL_AUTHOR``)."""
        if self.session is not None:
            return self.session.author_kind
        return session_identity.CHANNEL_AUTHOR[session_identity.CHANNEL_MCP]

    def _claim_entry(self, receipt: dict[str, Any]) -> dict[str, Any]:
        return {"case_id": receipt["case_id"], "seq": receipt["seq"], "kind": receipt["kind"],
                "claim": self._claim_out(receipt["case_id"], receipt["claim"])}

    def _claim_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        return {**self._claim_entry(receipt), "business_system_writes": 0}

    def _objective_receipt(self, result: dict[str, Any]) -> dict[str, Any]:
        """An objective's claim mode: each case done, in claim order, and each refused."""
        return {"cases": [self._claim_entry(receipt) for receipt in result["cases"]],
                "refused": result["refused"], "business_system_writes": 0}

    def _claim_seen(self, claim: dict[str, Any]) -> dict[str, Any]:
        """A case's claim as this session reads it: whether it is the active holder."""
        return {**claim, "held_by_this_session": bool(
            claim["active"] and claim["holder"] == self._claim_holder)}

    def _claim_out(self, case_id: str, claim: dict[str, Any]) -> dict[str, Any]:
        """A claim outside a case view: as seen by this session, labelled for a client."""
        seen = self._claim_seen(claim)
        return _label_claim(case_id, seen) if _TO_CLIENT.get() else seen

    def _records_authors(self) -> bool:
        """With an action path and the host's session, cases are opened and proposals
        recorded through the action path (``ActionPath.open_case`` / ``record_proposal``):
        the session's subject is their recorded opener and author, a subject of every
        action on them who may never approve one (decision g2-1). Without either, their
        author is unknown and no approval of an action on them can count."""
        return self.action_path is not None and self.session is not None

    def _guard_verdict(self, runtime: CaseRuntime, actor: Any, capability: str, payload: Any,
                       effect: Any, citations: list[Any]) -> dict[str, Any]:
        self._capability_offered(capability)
        verdict = capability_guards.check(capability, payload, effect,
                                          [str(c) for c in citations if c],
                                          capability_guards.current_reader(runtime, actor))
        if not verdict["passed"]:
            failure = verdict["failures"][0]
            raise InputError(("capability_guard_failed:" + ",".join(
                item["id"] for item in verdict["failures"]) + ": " + failure["reason"])[:400])
        return verdict

    def _capability_offered(self, capability: Any) -> None:
        # Fail closed: the name must already be canonical and declared in
        # operating/action_catalog.json as an owner-system proposal type.
        if capability_guards.canonical(capability) is None:
            raise InputError("capability_not_canonical: dot-separated lower-case words, "
                             "no surrounding space")
        if not capability_guards.declared(capability):
            raise InputError("capability_undeclared: declare it with its guards in "
                             "operating/action_catalog.json")
        if not capability_guards.available(capability, test_host=self.test_capabilities):
            raise InputError("capability_not_available_on_this_host: declared for test "
                             "hosts only")

    @staticmethod
    def _open_case(runtime: CaseRuntime, actor: Any, case_id: str) -> dict[str, Any]:
        """The case view; it must be readable, intact and open before anything is read
        or stored."""
        current = runtime.cases.get(actor, case_id)
        if current["status"] == "closed":
            raise InputError("case_closed")
        if not current["integrity"]["chain_ok"]:
            raise InputError("case_integrity_error: case_event_chain_broken")
        return current

    def _attach_original(self, runtime: CaseRuntime, actor: Any,
                         arguments: dict[str, Any]) -> dict[str, Any]:
        case_id = self._case_id(arguments)
        inbox_name = _arg(arguments, "inbox_name", str, required=True, lower=1, upper=200)
        principal = _arg(arguments, "inbox_principal", str, default=None, lower=1, upper=200)
        self._open_case(runtime, actor, case_id)
        try:
            original = runtime.originals.attach(actor, inbox_name, principal)
        except OriginalError as exc:
            raise InputError(str(exc)[:400]) from None
        extraction = original["extraction"]
        text = (f"text {extraction['status']} via {extraction['method']}"
                if extraction["method"] else f"text unavailable ({extraction['status']})")
        ocr = (f"; OCR (machine-read, to be confirmed) on page(s) "
               f"{', '.join(str(page) for page in extraction['ocr_pages'])}"
               if extraction["ocr_pages"] else "")
        statement = (f"Operator-supplied original '{original['filename']}' attached: "
                     f"{original['bytes']} bytes, sha256 {original['sha256']}, "
                     f"{_pages_read(extraction)}, {text}{ocr}.")
        finding = runtime.cases.finding(actor, case_id, statement, "fact", "supported",
                                        [original["citation"]], workbench_statement=True)
        return {"original": original, "finding": finding, "business_system_writes": 0,
                "next_tools": ["plx.case_add_finding (cite original://...#page=N)",
                               "plx.case_record_gap"]}

    def _reextract_original(self, runtime: CaseRuntime, actor: Any,
                            arguments: dict[str, Any]) -> dict[str, Any]:
        case_id = self._case_id(arguments)
        handle = _arg(arguments, "citation", str, required=True, lower=8, upper=200)
        reason = _arg(arguments, "reason", str, required=True, lower=1, upper=1000)
        if not handle.startswith("original://"):
            raise InputError("citation_scheme_not_supported: an original:// citation "
                             "from plx.case_attach_original is required")
        current = self._open_case(runtime, actor, case_id)
        try:
            version = runtime.originals.reextract(actor, handle, reason)
        except OriginalError as exc:
            raise InputError(str(exc)[:400]) from None
        result = {"extraction_version": version, "business_system_writes": 0,
                  "duplicate": version["duplicate"]}
        if version["duplicate"] and (version["version"] == 1 or self._case_cites(
                current, version["citation"])):
            result.update(finding=None, note=(
                f"the extraction matches version {version['version']}, already on this "
                "case; nothing recorded"))
            return result
        # A new version, or an existing one this case has no finding for yet
        # (another case re-extracted it, or an earlier finding write failed).
        extraction, previous = version["extraction"], version["previous_extraction"]

        def describe(summary: dict[str, Any]) -> str:
            text = (f"{summary['status']} via {summary['method']}" if summary["method"]
                    else f"unavailable ({summary['status']})")
            if summary["ocr_pages"]:
                text += (" with OCR (machine-read, to be confirmed) on page(s) "
                         + ", ".join(str(page) for page in summary["ocr_pages"]))
            return f"{_pages_read(summary)}, text {text}"

        statement = (f"Original sha256 {version['sha256']} re-extracted as extraction "
                     f"version {version['version']} ({describe(extraction)}); version "
                     f"{version['previous_version']} read {describe(previous)}. Same "
                     f"bytes; earlier citations unchanged.")
        result["finding"] = runtime.cases.finding(
            actor, case_id, statement, "fact", "supported",
            [SCHEME_ORIGINAL + version["original_id"], version["citation"]],
            workbench_statement=True)
        result["next_tools"] = ["plx.read (a page citation of the new version)",
                                "plx.case_add_finding"]
        return result

    # ---- live sources in cases ----------

    def _attach_sharepoint_original(self, runtime: CaseRuntime, actor: Any,
                                    arguments: dict[str, Any]) -> dict[str, Any]:
        """``plx.case_attach_original`` with drive_id + item_id: a SharePoint drive item
        read for the actor through the GET-only reader, stored as an original."""
        case_id = self._case_id(arguments)
        drive_id = _arg(arguments, "drive_id", str, required=True, lower=3, upper=200)
        item_id = _arg(arguments, "item_id", str, required=True, lower=1, upper=200)
        max_bytes = _arg(arguments, "max_bytes", int, default=None, lower=1, upper=100_000_000)
        self._open_case(runtime, actor, case_id)
        try:
            original = runtime.attach_sharepoint_original(actor, drive_id, item_id,
                                                          max_bytes=max_bytes)
        except OriginalError as exc:
            raise InputError(str(exc)[:400]) from None
        extraction = original["extraction"]
        provenance = original["provenance"]
        text = (f"text {extraction['status']} via {extraction['method']}"
                if extraction["method"] else f"text unavailable ({extraction['status']})")
        ocr = (f"; OCR (machine-read, to be confirmed) on page(s) "
               f"{', '.join(str(page) for page in extraction['ocr_pages'])}"
               if extraction["ocr_pages"] else "")
        statement = (f"SharePoint original '{provenance.get('path')}' (drive item "
                     f"{provenance['item_id']}, eTag {provenance['etag']}) attached: "
                     f"{original['bytes']} bytes, sha256 {original['sha256']}, "
                     f"{_pages_read(extraction)}, {text}{ocr}.")
        finding = runtime.cases.finding(actor, case_id, statement, "fact", "supported",
                                        [original["citation"],
                                         original["source_metadata_citation"]],
                                        workbench_statement=True)
        return {"original": original, "finding": finding, "business_system_writes": 0,
                "next_tools": ["plx.read (original://...#page=N)",
                               "plx.case_add_finding (cite original://...#page=N)",
                               "plx.case_attach_original (citation + reason re-extracts)"]}

    @staticmethod
    def _case_cites(view: dict[str, Any], handle: str) -> bool:
        """Whether any finding on the case (in any bucket) cites ``handle``."""
        return any(cite.get("handle") == handle for items in view["findings"].values()
                   for finding in items for cite in finding.get("citations", ()))
