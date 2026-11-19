"""A read request whose connection closes (the browser left the page, reloaded it, or
gave up) cancels the work done for it (``call_limits.Cancel``).

The routes are synchronous: their thread waits for a read worker's job and never sees the
connection. ``CancelOnDisconnect`` (ASGI, outermost) gives each GET its own ``Cancel`` in
the request's context (copied into the route's thread), listens for the server's
``http.disconnect`` beside the request and sets it when the connection closes before the
answer was sent. ``Worker.run`` drops a job still queued for it and cuts the statements of
one running (``seconds_left`` 0, the running Postgres statement cancelled on the server).

Only reads: a write (POST, PUT, DELETE) runs to its end whether or not the person still
waits (its journal and its outcome must not depend on the connection). A read others
share (``_screen_read``: joined by the reader's next requests, its page kept for them)
runs as no request's (``cancel_scope(None)``), so a reload is served by the read the
dropped request started."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from operating import call_limits

READ_METHODS = ("GET", "HEAD")


class CancelOnDisconnect:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or scope.get("method") not in READ_METHODS:
            await self.app(scope, receive, send)
            return
        first = await receive()
        if first.get("type") == "http.disconnect":
            return   # gone before it was read: nothing to answer
        if first.get("more_body"):
            # a read with a streamed body (no route takes one): passed on as it came
            replayed = False

            async def as_came() -> dict:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return first
                return await receive()
            await self.app(scope, as_came, send)
            return
        cancel = call_limits.Cancel()
        gone = asyncio.Event()
        answered = False

        async def listen() -> None:
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    if not answered:   # after the answer the close is the normal end
                        cancel.set()
                    gone.set()
                    return

        handed = False

        async def replay() -> dict:
            nonlocal handed
            if not handed:
                handed = True
                return first
            await gone.wait()
            return {"type": "http.disconnect"}

        async def sending(message: dict) -> None:
            nonlocal answered
            if message.get("type") == "http.response.body" and not message.get("more_body"):
                answered = True
            await send(message)

        listener = asyncio.ensure_future(listen())
        try:
            with call_limits.cancel_scope(cancel):
                await self.app(scope, replay, sending)
        finally:
            listener.cancel()
