"""The Graph subscription renewer's one sender (integrations/graph_subscriptions.py): the
Entra client-credentials token POST and requests to Graph's ``/subscriptions`` resource,
nothing else. A subscription object is a notification registration; it holds no mail, chat
or file.

``send_subscription`` refuses before any request a URL or method that is not one of:

* POST ``TOKEN_PREFIX`` + ``<tenant uuid>/oauth2/v2.0/token``;
* GET or POST ``SUBSCRIPTIONS_URL`` (list, create);
* GET or PATCH ``SUBSCRIPTIONS_URL`` + ``/<subscription id>`` (read, renew). There is no
  DELETE: removing a subscription cannot be sent through this module.

No query string, no other Graph path, redirects refused. Pinned by the HTTP-egress gate
(``scripts/check_http_egress.py``: ALLOWED and SENDER_URLS) to this function and these two
prefixes. The only Graph write path in the Workbench.
"""

from __future__ import annotations

import re
from typing import Mapping
import urllib.error
import urllib.request

TOKEN_PREFIX = "https://login.microsoftonline.com/"
SUBSCRIPTIONS_URL = "https://graph.microsoft.com/v1.0/subscriptions"
_TOKEN_PATH = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                         r"[0-9A-Fa-f]{12}/oauth2/v2\.0/token")
_ID = re.compile(r"/[A-Za-z0-9-]{1,128}")


class SenderRefused(Exception):
    """A request not made (an unlisted URL or method) or refused (a redirect)."""


def allowed(method: str, url: str) -> bool:
    if url.startswith(TOKEN_PREFIX):
        return method == "POST" and _TOKEN_PATH.fullmatch(url[len(TOKEN_PREFIX):]) is not None
    if url == SUBSCRIPTIONS_URL:
        return method in ("GET", "POST")
    if url.startswith(SUBSCRIPTIONS_URL):
        return method in ("GET", "PATCH") and _ID.fullmatch(
            url[len(SUBSCRIPTIONS_URL):]) is not None
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        raise SenderRefused("redirect refused")


def send_subscription(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                      timeout: float) -> tuple[int, bytes]:
    """One request to an allowed URL; (status, at most 256 KB of the reply). An HTTP error is
    its status; no connection raises ``OSError``."""
    if not allowed(method, url):
        raise SenderRefused("url or method not allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as reply:
            return reply.status, reply.read(262144)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, b""
