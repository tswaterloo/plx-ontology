"""The live Ask canary's one sender (``operating.ask_canary``): requests to the Workbench's
own API on this host's loopback, nothing else.

``canary_request`` refuses before any request anything but:

* ``GET`` of ``API_BASE`` + ``me`` (is the kept session still signed in);
* ``POST`` of ``API_BASE`` + ``auth/dev-login`` (the canary service session's minted ticket);
* ``POST`` of ``API_BASE`` + ``ask`` (the canary's one question).

Redirects are refused, and no proxy is used whatever the environment says (no
``ProxyHandler`` from ``*_proxy`` variables): the request goes to the loopback or nowhere. It reaches no business system: the API it asks is the Workbench's,
which answers from what it already holds and confirms nothing (an Ask never sends or
writes). Pinned by the HTTP-egress gate (``scripts/check_http_egress.py``: ALLOWED and
SENDER_URLS) to this function and this one base URL.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional
import urllib.error
import urllib.request

API_BASE = "http://127.0.0.1:8780/api/"
ROUTES = {("GET", "me"), ("POST", "auth/dev-login"), ("POST", "ask")}


class SenderRefused(Exception):
    """A request not made (an unlisted route) or refused (a redirect)."""


def allowed(method: str, url: str) -> bool:
    return url.startswith(API_BASE) and (method, url[len(API_BASE):]) in ROUTES


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        raise SenderRefused("redirect refused")


def canary_request(method: str, url: str, headers: Mapping[str, str],
                   body: Optional[bytes], timeout: float
                   ) -> tuple[int, Mapping[str, str], Iterable[bytes]]:
    """One request to an allowed route: (status, lower-cased headers, the reply's lines,
    read as they stream). An HTTP error is its status with no lines."""
    if not allowed(method, url) or (method == "GET") != (body is None):
        raise SenderRefused("route not allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        reply = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect
                                            ).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, {}, iter(())
    return reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply
