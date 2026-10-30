"""The one sender of the embeddings provider: a POST to the OpenAI embeddings endpoint.

Approved 2026-10-29: business text may be sent to OpenAI for embeddings. Only ``API_URL`` is ever reached; any other URL raises
before a request is built, redirects are refused, and ``scripts/check_http_egress.py``
pins this file to that URL and this function (``SENDER_URLS`` / ``ALLOWED``). It reaches
no business system: the body carries chunk text the caller already read. The caller
(``operating.embed.models._openai``) refuses to call it unless remote embedding is
switched on (``PLX_EMBED_ALLOW_REMOTE=1``) and never logs the key or the text.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Mapping

API_URL = "https://api.openai.com/v1/embeddings"


class TransportError(Exception):
    def __init__(self, code: str, status: int | None = None,
                 retry_after: float | None = None, error_code: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.retry_after = retry_after
        self.error_code = error_code   # the API's own error code ("insufficient_quota")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise TransportError("redirect_refused")


def post_embeddings(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> bytes:
    """POST ``body`` to ``API_URL`` (redirects refused) and return the response body. An
    HTTP error is a ``TransportError`` with its status and Retry-After seconds."""
    if url != API_URL:
        raise TransportError("url_not_allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        status = error.code
        retry = error.headers.get("retry-after") if error.headers else None
        try:
            detail = json.loads(error.read(4096) or b"{}").get("error") or {}
            code = detail.get("code") if isinstance(detail, dict) else None
        except (ValueError, OSError, AttributeError):
            code = None
        error.close()
        try:
            wait = float(retry) if retry is not None else None
        except ValueError:
            wait = None
        raise TransportError(f"http_{status}", status, wait,
                             code if isinstance(code, str) else None) from None
    except urllib.error.URLError as error:
        raise TransportError("network_error") from error
    with response:
        return response.read()


__all__ = ["API_URL", "TransportError", "post_embeddings"]
