"""Public Microsoft Graph change-notification webhook (``POST /graph/notify``).

Microsoft Graph posts two kinds of requests to this endpoint without user session cookies
or bearer tokens:

1. **Validation handshake**: ``POST /graph/notify?validationToken=<token>`` -> immediate
   ``200 OK`` with ``Content-Type: text/plain; charset=utf-8`` and the exact decoded token.
2. **Notification / lifecycle delivery**: JSON ``{"value": [...]}`` -> constant-time
   ``clientState`` check against ``PLX_GRAPH_NOTIFY_CLIENT_STATE_SECRET`` (fail closed when
   unset/empty), ``tenantId`` check, optional ``PLX_GRAPH_NOTIFY_SUBSCRIPTIONS`` allowlist,
   and immediate ``202 Accepted`` ``{"accepted": N, "dropped": M}`` while status and wake
   markers are written asynchronously in a background task.

Never logs ``clientState``, ``validationToken``, tenant/subscription IDs, resource paths,
or raw message content.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from fastapi import BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from integrations import graph_notifications as gn

GRAPH_NOTIFY_PATH = "/graph/notify"
GRAPH_NOTIFY_BODY_MAX = gn.WEBHOOK_MAX_BYTES


def resolve_state_dir(backend_state_dir: Path | str | None,
                      environ: Mapping[str, str] | None = None) -> Path | None:
    """State directory for ``graph-notify.json`` and wake markers."""
    if backend_state_dir is not None:
        return Path(backend_state_dir)
    env = os.environ if environ is None else environ
    raw = (env.get("PLX_INGEST_STATE") or "").strip()
    return Path(raw) if raw else None


def handle_graph_notify(
    body: bytes,
    *,
    state_dir: Path | None,
    background_tasks: BackgroundTasks,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> JSONResponse:
    """Validate a Graph webhook notification batch and schedule async status/wake writes."""
    env = os.environ if environ is None else environ
    settings = gn.Settings.for_webhook(env)
    result = gn.evaluate_webhook_body(body, settings)
    if state_dir is not None:
        background_tasks.add_task(gn.record_webhook_result, state_dir, result, now=now)
    return JSONResponse(
        {"accepted": result.accepted, "dropped": result.dropped},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


def register_graph_notify_route(app: Any, backend: Any) -> None:
    """Mount ``POST /graph/notify`` (excluded from OpenAPI schema)."""

    @app.post(GRAPH_NOTIFY_PATH, include_in_schema=False)
    async def graph_notify(
        request: Request,
        background_tasks: BackgroundTasks,
        validationToken: str | None = Query(None),
    ) -> Response:
        if validationToken is not None:
            return PlainTextResponse(
                content=validationToken,
                status_code=200,
                media_type="text/plain; charset=utf-8",
            )
        body = await request.body()
        state_dir = resolve_state_dir(getattr(backend, "state_dir", None))
        return handle_graph_notify(
            body,
            state_dir=state_dir,
            background_tasks=background_tasks,
        )
