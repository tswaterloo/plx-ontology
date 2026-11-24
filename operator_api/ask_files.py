"""The files of a question: sent by upload id (``operating.ask_uploads``) or inline, as the
items ``operating.ask_attachments.decode`` reads. One place for the Ask and the instruction
engine, whichever backend class they run on."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterator, Optional

from operating import ask_uploads


def store(backend: Any) -> ask_uploads.Uploads:
    """The backend's store of streamed uploads (under its state dir), made once."""
    got = getattr(backend, "_ask_uploads_store", None)
    if got is None:
        got = backend._ask_uploads_store = ask_uploads.Uploads.for_state_dir(
            getattr(backend, "state_dir", None))
    return got


def items(backend: Any, request: Any, reader: Optional[Any]) -> list[dict[str, Any]]:
    """The request's attachments for ``decode``: a file sent inline as it is, a file sent by
    upload id as the path of what this reader streamed in (another reader's id, a removed or
    expired one is a file ``missing``, said to the person)."""
    out: list[dict[str, Any]] = []
    for a in request.attachments:
        if a.upload is None:
            out.append({"name": a.name, "data": a.data})
            continue
        got = store(backend).get(reader.key, a.upload) if reader is not None else None
        out.append({"name": a.name, "path": str(got[0])} if got is not None
                   else {"name": a.name, "missing": True})
    return out


async def receive(chunks: AsyncIterator[bytes], uploads: ask_uploads.Uploads, owner: str,
                  name: str) -> dict[str, Any]:
    """Stream ``chunks`` (a request's body) into ``uploads`` on a worker thread and return the
    new upload. A body that ends in an error (the client went away) reaches the worker as
    that error, so the worker ends at once and the half-written file is removed (never kept as
    if whole); a refusal by the disk ends the feed too."""
    from starlette.concurrency import run_in_threadpool
    queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=8)
    loop = asyncio.get_running_loop()

    async def feed() -> None:
        try:
            async for chunk in chunks:
                if chunk:
                    await queue.put(chunk)
            await queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # the client went away: told to the worker, never silent
            await queue.put(exc)

    def drain() -> Iterator[bytes]:
        while True:
            chunk = asyncio.run_coroutine_threadsafe(queue.get(), loop).result()
            if chunk is None:
                return
            if isinstance(chunk, BaseException):
                raise ConnectionError("the upload was cut off before it ended") from chunk
            yield chunk
    reading = asyncio.ensure_future(feed())
    try:
        return await run_in_threadpool(uploads.put, owner, name, drain())
    finally:
        reading.cancel()
