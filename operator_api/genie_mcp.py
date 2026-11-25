"""Read-only Genie MCP (JSON-RPC 2.0) endpoint for external tools.

Exposes governed measures, business views/catalog, and measure execution
strictly through the authenticated ``Reader`` and their Postgres ``Door``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, Mapping, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "genie-mcp", "version": "1.0.0"}
MEASURES_RESOURCE_URI = "genie://measures"

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "genie_list_measures",
        "description": (
            "List governed business measures with their definitions, units, "
            "dimensions, and parameters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "genie_run_measure",
        "description": (
            "Execute one governed measure by id through the reader's guarded "
            "Postgres door, returning values, citations, SQL, and as_of."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Governed measure id."},
                "period": {"type": "string", "description": "Optional calendar period."},
                "days": {"type": "integer", "description": "Optional trailing days."},
                "by": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Optional breakdown dimension(s).",
                },
                "where": {
                    "type": "object",
                    "description": "Optional dimension equality filters.",
                },
                "compare": {"type": "boolean", "description": "Compare against prior period."},
                "against": {"type": "string", "description": "Comparison period."},
                "parameters": {
                    "type": "object",
                    "description": "Optional measure parameter overrides.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "genie_list_views",
        "description": "List the business views / data catalog visible to the reader.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

_RESOURCES: list[dict[str, Any]] = [
    {
        "uri": MEASURES_RESOURCE_URI,
        "name": "Governed Measures Catalog",
        "description": "Governed measures catalog JSON (including live overlay).",
        "mimeType": "application/json",
    }
]


def _rpc_result(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "result": result},
        headers={"Cache-Control": "no-store"},
    )


def _rpc_error(req_id: Any, code: int, message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _is_full_reader(who: Any) -> bool:
    from .feedback import owner_open

    return bool(owner_open(getattr(who, "session", None)))


def _list_measures(who: Any) -> dict[str, Any]:
    from operating import measures as ms

    listed = ms.listing(full=_is_full_reader(who))
    entries = ms.measures()
    out: list[dict[str, Any]] = []
    for item in listed.get("measures", []):
        mid = item.get("id")
        entry = entries.get(mid, {})
        params = ms.parameters_of(entry) if entry else []
        record: dict[str, Any] = {
            "id": mid,
            "name": item.get("name"),
            "definition": item.get("definition"),
            "unit": item.get("unit"),
            "view": item.get("source"),
            "dimensions": item.get("dimensions", []),
            "period": item.get("period"),
        }
        if params:
            record["parameters"] = [
                {"id": p.get("id"), "default": p.get("default", False), "says": p.get("says", "")}
                for p in params
            ]
        out.append(record)
    return {"measures": out}


def _list_views(backend: Any, who: Any) -> dict[str, Any]:
    from operating.pg import business_views as bv

    views_out: list[dict[str, Any]] = []
    for name, view in sorted(bv.listed().items()):
        doc = getattr(view, "doc", "") or ""
        views_out.append(
            {
                "name": name,
                "description": doc.strip().splitlines()[0] if doc.strip() else "",
            }
        )
    return {"views": views_out}


def _run_measure(backend: Any, who: Any, args: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    measure_id = args.get("id")
    if not isinstance(measure_id, str) or not measure_id.strip():
        raise ValueError("id must be a non-empty string")

    from . import insights as ins

    worker = getattr(backend, "reads", None) or getattr(backend, "access_worker", None)
    if worker is None:
        raise RuntimeError("measure execution is not available on this backend")

    call_args: dict[str, Any] = {"measure": measure_id.strip()}
    for key in ("period", "days", "by", "compare", "against", "parameters"):
        if args.get(key) is not None:
            call_args[key] = args[key]
    if args.get("where") is not None:
        call_args["filter"] = args["where"]

    result = worker.run(
        who,
        lambda view: view._call("plx.sql", call_args),
        timeout=ins.RUN_SECONDS,
    )
    as_of = dt.datetime.now(dt.timezone.utc).isoformat()
    if isinstance(result, Mapping):
        payload = dict(result)
        is_error = "error" in payload
        payload.setdefault("measure", measure_id.strip())
        payload.setdefault("as_of", as_of)
        return payload, is_error
    return {"measure": measure_id.strip(), "result": result, "as_of": as_of}, False


def _tool_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
        "isError": is_error,
    }


async def handle_mcp_request(request: Request, backend: Any, who: Any) -> JSONResponse:
    """Handle one MCP JSON-RPC 2.0 POST request for an authenticated ``Reader``."""
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, PARSE_ERROR, "Parse error", status_code=400)

    if not isinstance(body, Mapping) or body.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request", status_code=400)

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if not isinstance(method, str):
        return _rpc_error(req_id, INVALID_REQUEST, "Method must be a string", status_code=400)
    if not isinstance(params, Mapping):
        return _rpc_error(req_id, INVALID_PARAMS, "Params must be an object", status_code=400)

    # JSON-RPC 2.0 notifications have no id
    if "id" not in body:
        return JSONResponse({"ok": True}, status_code=202, headers={"Cache-Control": "no-store"})

    if method == "initialize":
        return _rpc_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": _TOOLS})
    if method == "resources/list":
        return _rpc_result(req_id, {"resources": _RESOURCES})
    if method == "resources/read":
        uri = params.get("uri")
        if uri != MEASURES_RESOURCE_URI:
            return _rpc_error(req_id, INVALID_PARAMS, f"Unknown resource: {uri!r}")
        data = _list_measures(who)
        return _rpc_result(
            req_id,
            {
                "contents": [
                    {
                        "uri": MEASURES_RESOURCE_URI,
                        "mimeType": "application/json",
                        "text": json.dumps(data, ensure_ascii=False, default=str),
                    }
                ]
            },
        )
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, Mapping):
            return _rpc_error(req_id, INVALID_PARAMS, "Invalid tools/call parameters")
        if name == "genie_list_measures":
            return _rpc_result(req_id, _tool_result(_list_measures(who)))
        if name == "genie_list_views":
            return _rpc_result(req_id, _tool_result(_list_views(backend, who)))
        if name == "genie_run_measure":
            try:
                res, is_error = await asyncio.to_thread(_run_measure, backend, who, args)
                return _rpc_result(req_id, _tool_result(res, is_error=is_error))
            except Exception as exc:
                return _rpc_result(req_id, _tool_result({"error": str(exc)}, is_error=True))
        return _rpc_error(req_id, METHOD_NOT_FOUND, f"Unknown or disallowed tool: {name!r}")

    return _rpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")
