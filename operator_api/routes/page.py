"""``GET /api/page/{kind}/{key}``: a page from the served store (contract C2, speed specs 1.2),
one read through the reader's door decided ahead (``operator_api.served.read``). ``home`` takes
the session's person key, never the path's."""
from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from starlette.background import BackgroundTask

from .. import served
from ..errors import RecordNotFound, UserFacing

KINDS = ("record", "home")
NOT_READY = {"busy": "is busy", "timeout": "took too long", "not_ready": "is updating access"}


def register(app: FastAPI, backend: Any, reader: Callable[..., Any]) -> None:
    """The routes on ``app`` itself (FastAPI 0.162 keeps an included router as one opaque
    entry of ``app.routes``, which the route rule and the API contract would not see)."""

    # before the general route: a History tab is its own family, read by its own shape
    @app.get("/api/page/history/{key:path}", tags=["object"],
             responses={401: {}, 404: {}, 422: {}, 503: {}})
    def history(key: str, request: Request,
                cursor: str = Query("", max_length=40, description="next_cursor of the page "
                                    "before, for older changes"),
                who: Any = Depends(reader)) -> Response:
        """A record's History tab from the served store (``models.RecordHistory``'s shape),
        newest first, a page at a time; not found reads the same as a record the reader may
        not open (speed specs 9.3 V2)."""
        if not key or len(key) > 700:
            raise UserFacing("invalid_request", "That is not a record.", 422)
        served.PENDING.set(None)
        try:
            out = served.history(backend, who, key, cursor or None)
        except ValueError:
            raise UserFacing("invalid_request", "That is not a history cursor.", 422) from None
        except RecordNotFound:
            raise UserFacing("not_found", "No such record that you can open.", 404) from None
        except served.Unavailable as exc:
            if exc.code == "not_built":
                served.request_build(served_dsn(backend, who), "history", key)
            raise UserFacing("page_unavailable", "This record's history " + (
                "is being prepared" if exc.code == "not_built" else NOT_READY[exc.code])
                + "; try again in a moment.", 503) from None
        finally:
            request.state.plx_src = served.SRC.get()
        labelled = getattr(backend, "_labelled", None)   # a full-read grant: said, journalled
        if callable(labelled):
            out = labelled(who, out, "/api/page/history")
        pending = served.PENDING.get()
        return Response(out.model_dump_json(), media_type="application/json",
                        headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(served.write_request, *pending)
                        if pending else None)

    @app.get("/api/page/{kind}/{key:path}", tags=["object"],
           responses={401: {}, 404: {}, 503: {}})
    def page(kind: Literal["record", "home", "alias"], key: str, request: Request,
             sections: str = Query("", max_length=400),
             comms_view: str = Query("", max_length=40), comms_at: str = Query("", max_length=60),
             comms_handle: str = Query("", max_length=2000),
             who: Any = Depends(reader)) -> Response:
        """A page's stored items in one read: status ok / stale / not_run, its as-of and
        every section's items (each ``{v, withheld, col}``)."""
        if kind == "home":
            key = str(getattr(who, "key", "") or "")
        wanted = [s for s in sections.split(",") if s] or None
        served.PENDING.set(None)
        try:
            after = (comms_view, comms_at, comms_handle) if comms_view and comms_handle else None
            body, headers = served.read(backend, who, kind, key, wanted, comms_after=after)
        except served.Unavailable as exc:
            served.SRC.set("unavailable")
            raise UserFacing("page_unavailable", f"This page {NOT_READY[exc.code]}; try again "
                             "in a moment.", 503) from None
        finally:
            request.state.plx_src = served.SRC.get()
        pending = served.PENDING.get()
        return Response(body, media_type="application/json",
                        headers={"Cache-Control": "no-store", **headers},
                        background=BackgroundTask(served.write_request, *pending)
                        if pending else None)


def served_dsn(backend: Any, who: Any) -> str:
    """The store the reader's door reads (where a build request is written)."""
    return served._door_for(backend, who)[1]
