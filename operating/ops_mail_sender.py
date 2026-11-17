"""The ops mail's one sender (operating/ops_notify.py): the Entra client-credentials token
POST and the Graph ``sendMail`` POST from the fixed ops mailbox, nothing else.

``post_ops_mail`` refuses before any request a URL that is not one of the two shapes:

* ``TOKEN_PREFIX`` + ``<tenant id>/oauth2/v2.0/token``;
* ``GRAPH_PREFIX`` + ``<mailbox address>/sendMail``.

Redirects are refused. It reaches no business record: the body is the token form or one
mail of alarm lines to the configured recipients (``ops_notify`` builds both). Pinned by
the HTTP-egress gate (``scripts/check_http_egress.py``: ALLOWED and SENDER_URLS) to this
function and these two prefixes.
"""

from __future__ import annotations

import re
from typing import Mapping
import urllib.error
import urllib.request

TOKEN_PREFIX = "https://login.microsoftonline.com/"
GRAPH_PREFIX = "https://graph.microsoft.com/v1.0/users/"
_TOKEN_PATH = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                         r"[0-9A-Fa-f]{12}/oauth2/v2\.0/token")
_SEND_PATH = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/sendMail")


class SenderRefused(Exception):
    """A request not made (an unlisted URL) or refused (a redirect); names the step."""


def allowed(url: str) -> bool:
    if url.startswith(TOKEN_PREFIX):
        return _TOKEN_PATH.fullmatch(url[len(TOKEN_PREFIX):]) is not None
    if url.startswith(GRAPH_PREFIX):
        return _SEND_PATH.fullmatch(url[len(GRAPH_PREFIX):]) is not None
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        raise SenderRefused("redirect refused")


def post_ops_mail(url: str, headers: Mapping[str, str], body: bytes,
                  timeout: float) -> tuple[int, bytes]:
    """One POST of ``body`` to an allowed URL; (status, at most 64 KB of the reply). An HTTP
    error is its status; no connection raises ``OSError``."""
    if not allowed(url):
        raise SenderRefused("url not allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as reply:
            return reply.status, reply.read(65536)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, b""
