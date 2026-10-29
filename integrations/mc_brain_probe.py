"""Read-only live probe for Mission Control and the brain (counts/shapes only).

Run by the credentialed live verifier on reviewed ``main``::

    python3 -m integrations.mc_brain_probe --output EVIDENCE/live/mc-brain-probe.json

Environment (names only): ``MC_MCP_API_KEY``, ``MC_OPERATOR_EMAIL`` (or
``--operator-email``; must be on MC's ``PLX_MC_ALLOWED_USERS``), optional
``MC_BASE_URL``/``MC_REPO``; brain pair ``BRAIN_BASE_URL``+``BRAIN_API_KEY``
or fallback ``VMC_API_URL``+``VMC_API_KEY``.

Calls, all through the guards in ``plx_hub_guard`` (read tools / GET only):
``mc_self_check``, ``mc_get_context{depth:compact}``,
``mc_search_knowledge{q:<fixed harmless query>, limit:3}`` and brain
``GET agent/status``. Each MC call appends an ``mcp.tool.invoked`` audit row
in MC (reads are free but audited; the owner accepted those rows for the live
verifier). The probe's own allowlist ``PROBE_TOOLS``
holds only those three read tools: ``_ProbeMcClient`` refuses any other tool
name and every write before I/O, on top of the ``plx_hub_guard`` grammar, and
an import-time check fails if the allowlist ever names a non-read tool. Output holds only status codes, error
codes, key names, list lengths, a few booleans/enums and the MC service
principal id (needed to prove which principal the key maps to); never
secrets, the operator email, titles, snippets or task text. A failed step says
why with the sanitized ``UpstreamError`` diagnosis: ``layer`` (transport,
http, jsonrpc, tool, knowledge_status), ``http_status``, ``rpc_code``,
``upstream_code``, ``cause_class`` and ``attempts``; never a message or body.
``knowledge_status`` with ``error: upstream_error`` means MC answered HTTP 200
and its brain call failed (non-2xx or non-JSON); MC does not pass the brain's
own status on, so the ``brain agent/status`` step is the direct cross-check.

That one failure is MC's relay, not the knowledge itself: when it persists after
MC's retries the probe asks the brain directly (``GET agent/search``, the same
query, over the configured credential pair, ``brain.credential_pair``: which host
and key were used, never what served the answer). ``knowledge.fallback`` says
what happened: ``not_needed`` (MC answered or failed otherwise), ``answered``,
``failed`` or ``not_run`` (no brain pair). If the brain answers,
``knowledge.served_by`` is ``brain_fallback`` and the MC step is listed in
``blocked_upstream`` (cause ``upstream_outage``). If the brain fails too with an
outage (``UpstreamError.blocks``: HTTP 5xx, 408, 429, transport, timeout),
``served_by`` stays null and the MC step, the fallback and a brain
``agent/status`` that failed the same way are all listed there with their HTTP
status and code: the brain itself is out. Either way the verifier records the
check ``blocked``, never a pass, red after 24 h. A brain step refused with 401/403
(``auth_refused``: the key is refused) is not an outage: it is never listed as
blocked, so the check fails, while
the MC step stays blocked as ``upstream_outage``. ``causes`` is the headline: each
blocked step with its cause and each refused step as ``credential_refused``, in step
order, so both show side by side. Every other failure (a guard refusal, a malformed
answer, which is a failed step and never a crash, any other MC or brain failure)
stays a failure (``ok`` false, exit 1).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from .http import SourceUnavailable
from .live_source_guard import ReadOnlyGuardError
from .mc import MissionControlMcpClient
from .plx_hub_guard import (MC_APPROVABLE_WRITE_TOOLS, MC_READ_TOOLS, MC_WRITE_TOOLS,
                            BrainConfig, HubTransport, McConfig, UpstreamError,
                            upstream_blocks)
from .vmc import BrainReadClient


PROBE_QUERY = "PLX Agent Workbench"
# The only MC tools this probe may call: read tools (pack 04, section 3, R rows).
PROBE_TOOLS = frozenset({"mc_self_check", "mc_get_context", "mc_search_knowledge"})
if (not PROBE_TOOLS <= frozenset(MC_READ_TOOLS) or PROBE_TOOLS & MC_WRITE_TOOLS
        or PROBE_TOOLS & frozenset(MC_APPROVABLE_WRITE_TOOLS)):
    raise ImportError("mc_brain_probe PROBE_TOOLS must hold MC read tools only")


class _ProbeMcClient(MissionControlMcpClient):
    """The MC client restricted to ``PROBE_TOOLS``; writes are refused outright."""

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if name not in PROBE_TOOLS:
            raise ReadOnlyGuardError("tool is not on the mc_brain_probe read allowlist")
        return super().call_tool(name, arguments)

    def call_write_tool(self, name: str, arguments: Mapping[str, Any], **_kw: Any) -> Any:
        raise ReadOnlyGuardError("mc_brain_probe never calls an MC write tool")


def operator_email_source(environ: Mapping[str, str], default: str | None) -> str:
    """``env`` (MC_OPERATOR_EMAIL wins), ``spec_default`` or ``missing``; never the value."""
    if (environ.get("MC_OPERATOR_EMAIL") or "").strip():
        return "env"
    return "spec_default" if default and default.strip() else "missing"


def _shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return sorted(str(k) for k in value)
    return type(value).__name__


def _counts(data: Any) -> dict[str, int]:
    if not isinstance(data, Mapping):
        return {}
    return {str(k): len(v) for k, v in data.items() if isinstance(v, list)}


def _failure(name: str, error: BaseException, code: str, **extra: Any) -> dict:
    """A failed step: the same keys for every failure, so the k-th failed step's
    diagnosis lines up across the verifier's ``steps[*].<key>`` lists."""
    up = error if isinstance(error, UpstreamError) else None
    return {"call": name, "ok": False, "error": code, "error_class": type(error).__name__,
            "layer": up.layer if up else None, "http_status": up.status if up else None,
            "rpc_code": up.rpc_code if up else None,
            "upstream_code": up.upstream_code if up else None,
            "cause_class": up.cause_class if up else None,
            "attempts": up.attempts if up else None, **extra}


def _step(name: str, call: Callable[[], Any], summarize: Callable[[Any], dict]) -> dict:
    try:
        result = call()
        try:
            summary = summarize(result)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            # An answer the client accepted but the probe cannot read: no detail, it may
            # echo the body.
            return _failure(name, error, "malformed_answer")
    except ReadOnlyGuardError as error:
        return _failure(name, error, "refused_by_guard", detail=str(error)[:200])
    except UpstreamError as error:
        return _failure(name, error, error.code)
    except (SourceUnavailable, PermissionError, ValueError) as error:
        return _failure(name, error, type(error).__name__, detail=str(error)[:200])
    return {"call": name, "ok": True, **summary}


def _outage(step: Mapping[str, Any]) -> bool:
    """The step failed on an upstream outage (``UpstreamError.blocks``), not on our
    request, a refused credential, a guard refusal or a malformed body."""
    return not step["ok"] and upstream_blocks(step["layer"], step["error"],
                                              step["http_status"], step["rpc_code"])


def _refused(step: Mapping[str, Any]) -> bool:
    """The upstream refused the step's credential (HTTP 401/403)."""
    return not step["ok"] and step["error"] == "auth_refused"


def _block(report: dict, step: Mapping[str, Any], source: str, cause: str) -> None:
    """List a failed step in ``blocked_upstream`` under an enum-safe part name (the
    step's call with ``:`` for the space) with its declared ``cause``."""
    report["blocked_upstream"].append({
        "part": step["call"].replace(" ", ":"), "source": source,
        "http_status": step["http_status"], "error_code": step["error"], "cause": cause})


def _principal(payload: Any) -> str | None:
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    actor = meta.get("actor") if isinstance(meta, Mapping) else None
    value = actor.get("servicePrincipalId") if isinstance(actor, Mapping) else None
    return value if isinstance(value, str) and len(value) <= 80 else None


def run_probe(environ: Mapping[str, str], *, operator_email: str | None = None,
              operator_email_default: str | None = None,
              hub: HubTransport | None = None) -> dict[str, Any]:
    hub = hub or HubTransport(environ=environ)
    report: dict[str, Any] = {"probe": "mc-brain-read/v1", "steps": [], "missing_inputs": [],
                              "blocked_upstream": [],
                              "knowledge": {"served_by": None, "fallback": "not_needed"}}
    knowledge_step: dict | None = None
    if operator_email is None:
        source = operator_email_source(environ, operator_email_default)
        if source == "spec_default":
            operator_email = operator_email_default
    else:
        source = "argument"
    report["operator_email_source"] = source
    try:
        mc_config = McConfig.from_env(environ, operator_email=operator_email)
    except SourceUnavailable as error:
        mc_config = None
        report["missing_inputs"].append(str(error)[:200])
    if mc_config is not None:
        report["mc"] = {"host": mc_config.host, "repo_label": mc_config.repo}
        mc = _ProbeMcClient(mc_config, hub=hub)
        report["steps"].append(_step("mc_self_check", mc.self_check, lambda p: {
            "shape": _shape(p), "self_check_ok": p.get("ok"),
            "dataSource": p.get("dataSource") if p.get("dataSource") in ("seed", "live") else None,
            "boringGateMet": p.get("boringGateMet") if isinstance(p.get("boringGateMet"), bool)
            else None}))
        report["steps"].append(_step("mc_get_context", mc.get_context, lambda p: {
            "shape": _shape(p), "data_shape": _shape(p.get("data")),
            "counts": _counts(p.get("data")), "service_principal": _principal(p)}))
        knowledge_step = _step("mc_search_knowledge",
                               lambda: mc.search_knowledge(PROBE_QUERY, limit=3),
                               lambda p: {
            "data_shape": _shape(p.get("data", p)),
            "status": (p.get("data", p) or {}).get("status"),
            "hit_count": len((p.get("data", p) or {}).get("hits") or []),
            "hit_fields": sorted({k for h in (p.get("data", p) or {}).get("hits") or []
                                  if isinstance(h, Mapping) for k in h})})
        report["steps"].append(knowledge_step)
        if knowledge_step["ok"]:
            report["knowledge"]["served_by"] = "mc"
    # MC answered 200 and only its own brain call failed (after MC-side retries).
    relay_outage = (knowledge_step is not None and _outage(knowledge_step)
                    and knowledge_step["layer"] == "knowledge_status")
    try:
        brain_config = BrainConfig.from_env(environ)
    except SourceUnavailable as error:
        brain_config = None
        report["missing_inputs"].append(str(error)[:200])
    if brain_config is not None:
        report["brain"] = {"host": brain_config.host, "credential_pair": brain_config.pair}
        brain = BrainReadClient(brain_config, hub=hub)
        status_step = _step("brain agent/status", brain.status, lambda p: {
            "data_shape": _shape(p.get("data")),
            "available": p["data"].get("available"),
            "flag_names": _shape(p["data"].get("flags"))})
        report["steps"].append(status_step)
        if relay_outage:
            fallback = _step("brain agent/search", lambda: brain.search(PROBE_QUERY, limit=3),
                             lambda p: {"hit_count": len(p["data"]["results"]),
                                        "partial": p["data"]["partial"]})
            report["steps"].append(fallback)
            # A refused key is our credential, not an outage: never blocked, so a fail.
            brain_out = _outage(fallback)
            report["knowledge"]["fallback"] = "answered" if fallback["ok"] else "failed"
            if fallback["ok"]:
                report["knowledge"]["served_by"] = "brain_fallback"
            if fallback["ok"] or brain_out or _refused(fallback):
                _block(report, knowledge_step, "mission-control-mcp", cause="upstream_outage")
            if brain_out:
                # The brain itself is out: MC's relay and the direct read both failed.
                _block(report, fallback, "brain", cause="upstream_outage")
                if _outage(status_step):
                    _block(report, status_step, "brain", cause="upstream_outage")
    elif relay_outage:
        report["knowledge"]["fallback"] = "not_run"
    if brain_config is None or brain_config.pair != "brain":
        report["missing_inputs"].append(
            "BRAIN_API_KEY (brn_) for the R7 cutover: Vince issues it; VMC_API_KEY stops "
            "working for brain routes at R11")
    report["requests_sent"] = hub.requests_sent
    blocked = {part["part"].replace(":", " "): part["cause"]
               for part in report["blocked_upstream"]}
    report["causes"] = [
        {"part": s["call"].replace(" ", ":"),
         "cause": blocked.get(s["call"]) or "credential_refused"}
        for s in report["steps"] if s["call"] in blocked or _refused(s)]
    report["ok"] = bool(report["steps"]) and all(s["ok"] or s["call"] in blocked
                                                 for s in report["steps"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--operator-email", default=None,
                        help="MC admission header value (else env MC_OPERATOR_EMAIL); not printed")
    parser.add_argument("--operator-email-default", default=None,
                        help="used only when MC_OPERATOR_EMAIL is unset (the documented agent "
                             "operator alias); not printed")
    parser.add_argument("--output", type=Path, default=None, help="also write the JSON report here")
    args = parser.parse_args(argv)
    report = run_probe(os.environ, operator_email=args.operator_email,
                       operator_email_default=args.operator_email_default)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
