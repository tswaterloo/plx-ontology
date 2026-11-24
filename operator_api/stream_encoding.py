"""Compressed streamed answers: gzip with a flush after every chunk.

The proxy compresses JSON answers (deploy/Caddyfile), never a stream, so nothing is held back
in its compressor's buffer. The streams were then sent as plain text: the Insights dashboard
stream ~350 KB a visit (one JSON object per chart). Here the API compresses a stream itself and
ends every chunk it is given with a sync flush, so each line or event reaches the browser as
soon as it is written, exactly as before, at a tenth of the bytes. The proxy leaves an answer
that already carries Content-Encoding alone.

Compressed only when the request says it takes gzip (Accept-Encoding, q > 0); an absent,
refused (q=0) or unreadable header gets the plain stream, as before.
"""

from __future__ import annotations

import zlib
from typing import Iterable, Iterator, Mapping, Optional

# deflate level: 6 is zlib's default (a 350 KB stream costs a few ms); the flush per chunk
# costs more ratio than any level would win
LEVEL = 6


def takes_gzip(accept_encoding: Optional[str]) -> bool:
    """Whether an Accept-Encoding header accepts gzip (RFC 9110: a q of 0 refuses it; ``*``
    stands for any coding not named). Anything unreadable: False (the plain stream)."""
    if not accept_encoding or len(accept_encoding) > 512:
        return False
    named: dict[str, float] = {}
    for part in accept_encoding.split(","):
        bits = [b.strip() for b in part.split(";")]
        coding = bits[0].lower()
        if not coding:
            continue
        q = 1.0
        for param in bits[1:]:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    return False
                if not 0.0 <= q <= 1.0:
                    return False
        named[coding] = q
    if "gzip" in named:
        return named["gzip"] > 0
    if "x-gzip" in named:
        return named["x-gzip"] > 0
    return named.get("*", 0) > 0


def gzip_chunks(chunks: Iterable[bytes | str]) -> Iterator[bytes]:
    """``chunks`` gzip-encoded, each one complete (sync-flushed) when it is yielded. Closed
    early (the reader went away): ``chunks`` is closed too."""
    z = zlib.compressobj(LEVEL, zlib.DEFLATED, 31)   # wbits 31: the gzip container
    try:
        for chunk in chunks:
            data = chunk.encode() if isinstance(chunk, str) else chunk
            if not data:
                continue
            yield z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)
        yield z.flush()
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def stream_encoding(headers: Mapping[str, str], chunks: Iterable[bytes | str],
                    response_headers: dict[str, str]) -> Iterable[bytes | str]:
    """The body to stream for a request with ``headers``: gzip (and Content-Encoding added to
    ``response_headers``) when it takes gzip, else ``chunks`` unchanged. Vary is set either
    way, so no cache hands one reader's encoding to another."""
    response_headers["Vary"] = "Accept-Encoding"
    if not takes_gzip(headers.get("accept-encoding")):
        return chunks
    response_headers["Content-Encoding"] = "gzip"
    return gzip_chunks(chunks)
