"""The case host's errors, envelopes and result shaping (pages, rows, cases)."""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from typing import Any

from . import injection_guard
from .case_runtime import LIMITATIONS as CASE_LIMITATIONS
from .playbook_originals import extraction_skipped

from .mcp_server_catalog import CONTRACT_TYPING, SELF_ENVELOPING_TOOLS


class InputError(ValueError):
    """A model-correctable tool argument error."""


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message[:512]}], "isError": True}


# A refusal a client reads bare: one code, optionally ':' and Workbench names (_client_error).
_ERROR_CODE = re.compile(r"([a-z][a-z0-9_]{0,63})(?::([A-Za-z0-9_.=/@,-]{0,128}))?")


_ERROR_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63})(?:=[0-9]{1,12})?|[0-9]{1,12}")


# Codes whose token is itself a Workbench refusal code (operating.action_tools passes the
# action path's ActionPathError/ActionRefused code), never input or source text.
_CODE_OF_CODE = frozenset({"action_refused"})


_REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _client_error(message: str, tool: str, names: frozenset[str] = frozenset()) -> str:
    """Refusal text as it crosses the output boundary: a code stays as it is, with a token
    after ':' only when it is a Workbench name: each comma-separated item an argument
    ``names`` the tool declares (optionally ``=<number>``) or a number, or after a code of
    ``_CODE_OF_CODE`` one refusal code. Any other text (an exception message may echo what
    it refused, a source-derived token included) leaves as JSON naming its leading code,
    with the text as an ``untrusted_text`` envelope."""
    match = _ERROR_CODE.fullmatch(message)
    token = match.group(2) if match else None
    if match and (token is None or all(
            (item := _ERROR_NAME.fullmatch(part)) is not None
            and (item.group(1) is None or item.group(1) in names)
            for part in token.split(",")) or (
            match.group(1) in _CODE_OF_CODE and _REFUSAL_CODE.fullmatch(token))):
        return message
    code = re.match(r"[a-z][a-z0-9_]{0,63}(?=$|[:\s])", message)
    return json.dumps({"error": code.group(0) if code else "tool_error",
                       "message": injection_guard.UntrustedText(
                           message[:400], injection_guard.WORKBENCH_ORIGIN + tool).envelope()},
                      ensure_ascii=False, separators=(",", ":"))


def _pages_read(extraction: dict[str, Any]) -> str:
    """An attached original's pages as a finding states them: the pages read and, for a
    read capped by pages, the pages skipped and which (never the pages read as the file's)."""
    read = f"{extraction['page_count']} page(s)"
    skipped = extraction_skipped(extraction)
    if skipped is None:
        return read
    if "first_page" not in skipped:
        return f"{read} read, further pages skipped by the page cap"
    first, last = skipped["first_page"], skipped["last_page"]
    return (f"{read} read, {last - first + 1} page(s) skipped by the "
            f"{skipped.get('limit', first - 1)}-page cap (pages {first}-{last} not read)")


def _arg(arguments: dict[str, Any], name: str, kind: type,
         *, default: Any = None, required: bool = False,
         lower: int | None = None, upper: int | None = None) -> Any:
    if name not in arguments:
        if required:
            raise InputError(f"missing_argument:{name}")
        return default
    value = arguments[name]
    if kind is int:
        valid = type(value) is int
    else:
        valid = isinstance(value, kind)
    if not valid:
        raise InputError(f"invalid_argument_type:{name}")
    if kind is bool:
        return value  # booleans have no size; bounds do not apply
    size = value if kind is int else len(value)
    if lower is not None and size < lower or upper is not None and size > upper:
        raise InputError(f"argument_out_of_bounds:{name}")
    return value


_SUGGESTION_FIELDS = {"column": 64, "value": 255, "model": 120, "basis": 20, "quote": 500}


def _model_suggestions(values: list[Any]) -> list[dict[str, Any]]:
    """Validate model suggestions as the schema says: unknown fields are refused.

    Content checks (known column, allowed value, exact current span) stay with
    ``operating.doc_metadata``, which reports a failed one as rejected.
    """
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise InputError(f"invalid_argument_type:model_suggestions[{index}]")
        unknown = set(item) - set(_SUGGESTION_FIELDS) - {"span"}
        if unknown:
            raise InputError(f"unknown_arguments:model_suggestions[{index}]." +
                             ",".join(sorted(unknown))[:96])
        for field in ("column", "value", "model", "span", "quote"):
            if field not in item:
                raise InputError(f"missing_argument:model_suggestions[{index}].{field}")
        for field, upper in _SUGGESTION_FIELDS.items():
            if field in item and (not isinstance(item[field], str) or len(item[field]) > upper):
                raise InputError(f"invalid_argument_type:model_suggestions[{index}].{field}")
        span = item["span"]
        if (not isinstance(span, dict) or set(span) != {"start", "end"}
                or type(span["start"]) is not int or type(span["end"]) is not int):
            raise InputError(f"invalid_argument_type:model_suggestions[{index}].span")
    return values


FRESHNESS_META_KEY = "plx/freshness"


# The session's agent: the effective definition (id, version, digest, limits) and its
# run's counts and state, on every tool answer (a refusal is no answer and carries no
# meta: an agent refusal names its reason, operating.agent_definitions).
AGENT_META_KEY = "plx/agent"


# Bounded results leave this much room for the _meta: the freshness summary, the agent
# stamp (AGENT_META_KEY, with an escalation at most ~460 bytes) and an unkept evidence note
# (EVIDENCE_META_KEY, ~70 bytes) fit beside them.
META_RESERVE_BYTES = 1024


_FRESHNESS_UNIT_FIELDS = ("unit_id", "source_id", "kind", "state", "reasons", "lag",
                          "last_complete_age_seconds", "last_run_id", "items_current",
                          "counts_visible")


def _meta_summary(meta: dict[str, Any]) -> dict[str, Any]:
    """The freshness meta without its per-unit list (counted instead) and texts; it fits
    ``META_RESERVE_BYTES``."""
    if "units" not in meta:
        return meta
    return {"configured": meta["configured"], "status": meta["status"],
            "summary": meta["summary"], "units_withheld": meta["units_withheld"],
            "units_omitted": len(meta["units"])}


def _envelope_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


CASE_SECTIONS = ("summary", "header", "findings", "calculations", "gaps", "proposals",
                 "verifications", "links", "timeline")


_CASE_HEADER = ("case_id", "question", "roots", "owner", "tenant", "classification",
                "audience", "status", "opened_at", "updated_at", "closed_at", "closed_by",
                "outcome", "objective_author_kind")


_PREVIEW_CHARS = 300


# Whether the result being built crosses the output boundary (a client, possibly a
# model) or goes to in-process host code (``handle(host_code=True)``): it decides the size
# every fitting step measures, so host code pages exactly as the bare result needs.
_TO_CLIENT: ContextVar[bool] = ContextVar("plx_mcp_result_to_client", default=True)


# The tool whose result is being built: the origin of the Workbench's own text in it.
_TOOL: ContextVar[str | None] = ContextVar("plx_mcp_result_tool", default=None)


def _firewall(value: Any, tool: str | None = None) -> Any:
    """The output boundary (injection firewall): every string of a tool
    result that is not a typed field (``injection_guard.TYPED_FIELDS`` and their
    grammars) or a schema-typed cell of a row envelope, as an ``untrusted_text``
    envelope. Idempotent, so text already wrapped stays one envelope; digests
    (``page_text_sha256``, ``body_sha256``) are of the envelope's text, which is never
    cut silently. Text the Workbench wrote has the origin ``workbench://<tool>``. A
    self-enveloping tool's result keeps its own envelopes and contract strings."""
    contract = CONTRACT_TYPING if tool in SELF_ENVELOPING_TOOLS else None
    return injection_guard.firewall_tool_output(value, tool=tool, contract=contract)


def _without_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {name: _without_key(item, key) for name, item in value.items() if name != key}
    if isinstance(value, list):
        return [_without_key(item, key) for item in value]
    return value


def _is_original_view(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("citation"), str)
            and value["citation"].startswith("original://"))


def _omit_page_citations(value: Any) -> Any:
    """Replace every original view's page citation list by its pattern."""
    if isinstance(value, list):
        return [_omit_page_citations(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: _omit_page_citations(item) for key, item in value.items()}
    if _is_original_view(value) and isinstance(value.get("page_citations"), list):
        citation = value["citation"]
        del out["page_citations"]
        out.update(page_citations_omitted=True, page_citation_pattern=(
            citation + ("&" if "#" in citation else "#") + "page=<n>"))
    return out


def _omit_warnings(value: Any, inside: bool = False) -> Any:
    """Drop extraction warnings inside original views (their counts remain)."""
    if isinstance(value, list):
        return [_omit_warnings(item, inside) for item in value]
    if not isinstance(value, dict):
        return value
    inside = inside or _is_original_view(value)
    out = {key: _omit_warnings(item, inside) for key, item in value.items()}
    if inside and isinstance(value.get("warnings"), list):
        out.update(warnings=None, warnings_omitted=True, warning_count=len(value["warnings"]))
    return out


def _cell_text(value: Any) -> str | None:
    """A row cell as the text the output boundary marks: a string, or a JSON object or
    array serialized as ``injection_guard`` serializes it; None for a scalar."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return None


def _limitations_ref(where: list[str]) -> dict[str, Any]:
    """A reference to the case runtime limitations (count and digest of the list) for paged
    results that do not repeat the text on every page."""
    return {"count": len(CASE_LIMITATIONS), "sha256": hashlib.sha256(json.dumps(
        list(CASE_LIMITATIONS), ensure_ascii=False).encode("utf-8")).hexdigest(), "in": where}


def _preview(text: Any) -> Any:
    if injection_guard.is_envelope(text):
        short = _preview(text["untrusted_text"])
        return text if short == text["untrusted_text"] else {
            **text, "untrusted_text": short, "truncated": True}
    if isinstance(text, str) and len(text) > _PREVIEW_CHARS:
        return text[:_PREVIEW_CHARS] + "…"
    return text


CASE_ORIGIN = "case://"


def _label_case_text(view: dict[str, Any]) -> dict[str, Any]:
    """Case text a person or an agent wrote, as data with its true origin: the question
    (``case://<id>``, its recorded author kind), the outcome, and each finding's
    statement and gap's text (``case://<id>#F<n>``, ``#G<n>``, the finding's recorded
    author kind; ``unknown`` where none was recorded), never as text the Workbench
    wrote. A Workbench-written statement (``statement_by``) keeps its own origin
    (``injection_guard.firewall_tool_output``). Only for a client (``_TO_CLIENT``):
    host code reads the case as stored."""
    case_id = view["case_id"]

    def wrap(text: Any, ref: str | None, field: str, author: str) -> Any:
        if not isinstance(text, str):
            return text
        citation = f"{CASE_ORIGIN}{case_id}" + (f"#{ref}" if ref else "")
        return injection_guard.UntrustedText(text, citation, field,
                                             author_kind=author).envelope()

    findings = {bucket: [item if item.get("statement_by") == "workbench" else
                         {**item, "statement": wrap(item.get("statement"), item["id"],
                                                    "statement",
                                                    item.get("author_kind") or "unknown")}
                         for item in items]
                for bucket, items in view["findings"].items()}
    gaps = [gap if gap.get("statement_by") == "workbench" else
            {**gap, **{name: wrap(gap.get(name), gap["id"], name, "unknown")
                       for name in ("missing", "why")}}
            for gap in view["gaps"]]
    # A never-claimed case has neither claim nor handoff key.
    claimed = {"claim": _label_claim(case_id, view["claim"])} if "claim" in view else {}
    if view.get("handoff"):
        claimed["handoff"] = {**view["handoff"], "note": wrap(
            view["handoff"]["note"], None, "handoff",
            view["handoff"].get("author_kind") or "unknown")}
    return {**view, "findings": findings, "gaps": gaps, **claimed,
            "question": wrap(view["question"], None, "question",
                             injection_guard.objective_author_kind(view)),
            "outcome": wrap(view.get("outcome"), None, "outcome", "unknown")}


def _label_claim(case_id: str, claim: dict[str, Any]) -> dict[str, Any]:
    """A case's claim for a client: each break's reason as the text its approver's session
    wrote (``ActionPath.break_claim``: a person on the console, an agent over MCP; an
    unrecorded author reads unknown), never as Workbench text."""
    if not claim.get("breaks"):
        return claim
    return {**claim, "breaks": [{**item, "reason": injection_guard.UntrustedText(
        item["reason"], f"{CASE_ORIGIN}{case_id}", "claim_break_reason",
        author_kind=item.get("author_kind") or "unknown").envelope()}
        for item in claim["breaks"]]}
