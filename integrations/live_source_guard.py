"""Read-only guard, credentials, entitlements and citations for live Microsoft sources.

Shared by ``integrations.graph_read`` (Graph mail, SharePoint drive items and
content), ``integrations.capture_teams`` (Teams channel and chat messages) and
``integrations.bc_read`` (Business Central API v2.0, GET only), and by the two external
read-only sources of decision 2026-10-19-fba-external-sources: ``integrations.freightcom_read``
(Freightcom Customer API, test host ``customer-external-api.ssd-test.freightcom.com`` only, GET
only; the production host stays refused) and ``integrations.amazon_sp_read`` (Amazon SP-API
region hosts, GET only, plus the one Login with Amazon refresh-token POST to
``https://api.amazon.com/auth/o2/token``). Their queries pass a closed per-operation grammar
(``live_source_grammar.external_query_ok``).

Why this exists: the Entra application behind ``MICROSOFT_GRAPH_*`` / ``BC_*``
holds write and admin roles (including Mail.Send), so the source does not keep
us read-only. Read-only is enforced only here, before any I/O:

* ``GuardedTransport.request`` is the single entry point for every credentialed
  request. ``GET`` is the only method, except one ``POST`` to
  ``https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`` with a
  client-credentials body for an allowed ``.default`` scope. Hosts are exactly
  ``graph.microsoft.com``, ``login.microsoftonline.com`` and
  ``api.businesscentral.dynamics.com``; the path must parse into one operation
  of the closed request grammar (``live_source_grammar``: fixed templates with
  typed, validated parameters; every segment decoded once must carry no
  ``/``, ``\\``, ``%`` or ``..``);
  any action-like path segment (send, reply, forward, createUploadSession,
  move, copy, delete, BC ``Microsoft.NAV.*`` bound actions, ...) is refused;
  redirects are never followed; any URL named by an ``MC_GO_LIVE_TEAMS_*``
  variable is refused. Azure Document Intelligence and Azure Storage hosts are
  outside the allowlist.
* ``GuardedTransport.download_preauthenticated`` is the one credential-free
  exception: SharePoint file bytes are served only from the short-lived
  ``@microsoft.graph.downloadUrl`` on the tenant's ``*.sharepoint.com`` host.
  It sends no Authorization header, accepts only the one SharePoint host the
  transport was configured with (the host of ``SHAREPOINT_SITE_ID``; none
  configured -> every download refused) and that tenant's OneDrive host
  (``<tenant>-my.sharepoint.com``, personal-site drives), GET only, no
  redirects, a byte cap, and is requested only after the item passed the
  audience check. ``SourceApi.get_content`` reaches it from
  ``graph.drive_item_content``: that credentialed GET is the one request whose
  302 is returned instead of refused; its Location is held as a ``Secret`` and
  fetched only through ``download_preauthenticated``, never with the bearer.
* Bounds: per-request timeout (<= 60 s; a connect, proxy tunnel and TLS handshake
  get at most ``CONNECT_TIMEOUT_SECONDS`` of it) and response byte cap; listings have
  page and item caps and always carry a ``completeness`` block (F8). A caller may
  bound a transport's requests (GETs and token POSTs alike) with a
  ``RequestBudget``: while the budget is active in the caller's context, the
  request that would exceed it is refused before I/O (``RequestBudgetExhausted``,
  never retried), so no read inside it overshoots.
* Retry (opt-in per ``SourceApi``; BC uses it, Graph does not yet): a GET that
  failed before any status (``transport_failure``) or answered 429/502/503/504
  is resent at most twice through ``BoundedBackoff`` (jittered, capped at
  4 s, ``Retry-After`` honoured up to the cap, optional total budget; the
  real sender hands back an error response's ``Retry-After`` and no other
  header). The OAuth token POST and every other non-GET are never resent.
* Diagnosis: ``unavailable_detail`` maps a ``SourceUnavailable`` to one token
  of a fixed vocabulary (``http_429``, ``timeout``, ``nextlink_escaped``,
  ``retried.http_503`` ...); source text never passes. An HTTP failure also
  carries ``http_status`` and ``error_code`` (the body's Graph/BC/OAuth error
  code as a token of the fixed ``ERROR_CODE_TOKENS`` or of BC's platform code family,
  ``is_error_code_token``, else ``other``);
  ``failure_diagnostics`` gives those four fields and nothing else.
* Keep-alive (GET only): the real sender keeps a small, bounded, thread-safe pool of
  idle connections per connect host (and proxy tunnel target). A connection goes back
  only after its response was read whole to its framed end with no ``Connection:
  close``; an error status, redirect, timeout, partial or oversized read or any
  exception closes it. Every request is checked by ``_refuse_non_read_send`` before a
  connection is taken or opened; TLS settings are urllib's defaults, unchanged.
* Whole bodies only: the sender reads to EOF (still bounded) and refuses a
  body shorter than its ``Content-Length`` with ``DownloadIncomplete``
  ("download incomplete"); ``http.client`` would otherwise hand back a cut
  body without raising.
* Secrets (client secret, access tokens, download URLs) are held in
  ``Secret`` and never appear in exceptions, reprs, records or citations.

Entitlements (default deny): the app-only token sees every mailbox, site and
company, so the Workbench decides per employee before retrieval. A declared
``plx-live-source-audience/v1`` map (same semantics as the source mirror's
relation audience: exact or trailing-``*`` patterns, most specific wins,
tenant + clearance + principal intersection) names who may read each resource
(``mail:<mailbox>``, ``sharepoint:<drive-id>:<path>``,
``bc:<environment>:<company-id>:<entitySet>``, ``teams:channel:<team-id>:<channel-id>``,
``teams:chat:<chat-id>``, ``repo:<owner>/<name>``: exact patterns only). Resources listed in ``source_acl_required`` (and every
``teams:`` resource, always) additionally need the actor's declared source
identities to appear in the item's own source ACL (mailbox owner; SharePoint
item permissions; Teams channel or chat membership). An unreadable ACL denies.

Canonical identity before audience (changed diagnosis after review of
7c270ee): each reader has one guarded entry point per target that first
resolves the caller's name to the server's canonical identity with a
metadata-only GET (mail: ``/users/{x}?$select=id,userPrincipalName,mail,
proxyAddresses``; SharePoint: the folder/item Graph resolves -> server-derived
``parentReference.path`` + ``name`` and item id; BC: lower-case GUIDs), then
reads by id. An actor with no grant in a source family (``may_read_any``)
causes no I/O.

Redesigned contract after the second review round in the same family
(caller-shaped input reaching a resource the audience did not decide on):

A. Closed request grammar (``live_source_grammar``): every URL comes from a
   fixed per-operation template with typed, validated parameters; the guard
   accepts a GET only if its path parses back into a template, and refuses
   any segment whose single decoding contains ``/``, ``\\``, ``%`` or ``..``.
B. Identity sets, most restrictive wins (``LiveSourceAudience.allows_all``):
   a mailbox answers to ``mail:<x>`` for its object id, UPN, mail and every
   SMTP proxy address; a folder answers to ``/X`` and ``/X/``. Every matched
   entry must grant the actor and the strictest classification applies.
   Mail citations use the object id; SharePoint citations the item id.
C. Post-read provenance: a message must carry a ``parentFolderId`` that
   resolves under the resolved user (or equal the resolved folder); a child
   must carry ``parentReference.id`` = the resolved folder and this drive's
   ``driveId``. Others are dropped and counted (``provenance_dropped``).

SharePoint audience keys are drive-relative server paths
(``sharepoint:<drive-id>:/Customers/Acme``), never site-relative
(``/Shared Documents/...``) and without the library name; a deny written
site-relative never matches (fails open for that deny), so write keys as the
probe/records report ``path``.

Documented limitations (accepted for now, not yet live-verified):

* ``withheld_count`` discloses how many hidden items a *granted* container
  holds (honest F8 completeness; refused containers disclose nothing).
* nextLink must name the same collection: OData key syntax
  (``mailFolders('<id>')``, ``companies(<guid>)``) is canonicalised to path
  form on both sides, navigation words and GUIDs compare case-insensitively,
  opaque ids exactly; the GET keeps the request's own path and takes only the
  server's query. A Graph query may add only ``$skiptoken``, ``$skip``,
  ``$top`` (never above the request's ``$top``) and ``$deltatoken`` (bounded
  values) and repeat the request's own options with their exact values; a
  server that *drops* request options from the link is not detected (see
  ``_graph_continuation_ok``). A BC query must pass the grammar and repeat
  every request option with its value, adding only ``aid``, ``$skiptoken``
  and a ``$top`` no higher than the request's (``_bc_continuation_ok``; a
  dropped option refuses). An accepted continuation must itself pass
  ``check_request`` (no raw space, control character or malformed ``%``).
  Any other continuation fails closed (``nextlink_refused``) instead of
  reporting truncation. The page
  cap is checked first: a continuation the read will not follow is never
  inspected and the listing reports ``page_cap``.
* ``parentReference.path`` is percent-decoded once; a name that literally
  contains ``%xx`` may be mis-derived (it then fails closed or reads as
  another path that is itself audience-checked).
* The timeout is per socket operation, not an overall deadline.
* SharePoint usually lists ``quickXorHash``, not ``sha256Hash``, so the
  source-hash check rarely runs; the eTag re-read still binds the bytes.
* Group grants are not expanded to members; BC has no record ACL.
* ``integrations.graph_evidence`` (delta readers) routes every Graph GET and
  every pre-authenticated download through this guard (closed grammar,
  pinned SharePoint download host). Its single larger bound: a file
  attachment GET (``graph.message_attachment``) may return up to
  ``MAX_ATTACHMENT_RESPONSE_BYTES`` of base64 JSON (a 20 MB file).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import (parse_qs, parse_qsl, quote, unquote, unquote_to_bytes, urlencode,
                          urlsplit)
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead

from ontology.core import LEVELS, Actor

from . import live_source_grammar as grammar
from .http import SourceUnavailable


GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
BC_HOST = "api.businesscentral.dynamics.com"
# External read-only sources (decision 2026-10-19-fba-external-sources): the Freightcom
# test host, the Amazon SP-API region hosts and the Login with Amazon token host. The
# Freightcom production host is named only so tests can assert it stays refused.
FREIGHTCOM_TEST_HOST = grammar.FREIGHTCOM_TEST
FREIGHTCOM_PRODUCTION_HOST = "external-api.freightcom.com"
SP_API_HOSTS = frozenset(grammar.SP_API_HOSTS.values())
LWA_HOST = "api.amazon.com"
LWA_TOKEN_PATH = "/auth/o2/token"
LWA_FORM_KEYS = frozenset({"grant_type", "refresh_token", "client_id", "client_secret"})
TOKEN_HOSTS = frozenset({LOGIN_HOST, LWA_HOST})
ALLOWED_HOSTS = frozenset({GRAPH_HOST, LOGIN_HOST, BC_HOST, LWA_HOST, *grammar.EXTERNAL_HOSTS})
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
BC_SCOPE = "https://api.businesscentral.dynamics.com/.default"
ALLOWED_SCOPES = frozenset({GRAPH_SCOPE, BC_SCOPE})
# Action-like path tokens. Graph actions are POSTs, so the GET-only rule
# already blocks them; this list is defence in depth against a method mistake.
DENIED_PATH_TOKENS = frozenset({
    "send", "sendmail", "reply", "replyall", "forward", "createreply",
    "createreplyall", "createforward", "createuploadsession", "move", "copy",
    "delete", "permanentdelete", "restore", "invite", "createlink", "checkin",
    "checkout", "discardcheckout", "follow", "unfollow", "markasjunk",
    "markasnotjunk", "assignsensitivitylabel", "setretentionlabel",
    "removeretentionlabel", "lockorunlockrecord", "subscriptions", "$batch",
    "batch", "post", "release", "reopen", "cancel", "ship", "shipandinvoice",
    "receive", "receiveandinvoice", "makeinvoice", "makeorder", "grant",
    "revokegrants", "unsubscribe", "upload", "patch", "update", "create",
})
_TOKEN_SPLIT = re.compile(r"[./\\()=,'\s]+")
# Graph path addressing (root:/a/b:) carries folder names; they are data,
# percent-encoded by the caller, and are excluded from the action check.
_GRAPH_PATH_ADDRESS = re.compile(r"root:/[^:]*:")
_TENANT = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{12}|[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9][a-z0-9-]{0,62})+)$")
_TOKEN_PATH = re.compile(r"^/([^/]+)/oauth2/v2\.0/token$")
MAX_RESPONSE_BYTES = 8_000_000
# Operations answered with a schema document instead of JSON (``SourceApi.get_document``).
DOCUMENT_OPERATIONS = frozenset({"bc.metadata"})
# Only a single file-attachment GET may exceed MAX_RESPONSE_BYTES: Graph returns
# the file as base64 contentBytes inside JSON (20 MB file -> < 28 MB response).
MAX_ATTACHMENT_RESPONSE_BYTES = 28_000_000
_LARGE_RESPONSE_OPERATIONS = frozenset({"graph.message_attachment"})
# Bound of one credential-free pre-authenticated SharePoint download (one original,
# held in memory once). Delivered use-case originals exceed 25 MB (a 31 MB zip of
# case mails and PDFs, 02-CASES-AND-ARTIFACTS 1.11); callers pass their own lower cap.
MAX_CONTENT_BYTES = 100_000_000
LIVE_AUDIENCE_SCHEMA = "plx-live-source-audience/v1"


class ReadOnlyGuardError(PermissionError):
    """A request was refused by the read-only guard before any I/O."""


class DownloadIncomplete(SourceUnavailable):
    """The body ended before its declared length (connection cut in transit).

    Distinct from other unavailability so a caller may make one bounded,
    version-checked retry; the bytes received are never returned.
    """

    def __init__(self, message: str = "download incomplete") -> None:
        super().__init__(message)


class SourceNotFound(SourceUnavailable):
    """The source answered 404 for an exact identity."""


class RequestBudgetExhausted(SourceUnavailable):
    """A request refused before I/O: the active ``RequestBudget`` of its transport
    is spent. Not a transport failure, so it is never retried."""

    def __init__(self) -> None:
        super().__init__("request budget exhausted")


class RequestBudget:
    """At most ``limit`` requests on ``transports`` (one ``GuardedTransport`` or
    several, e.g. Graph and BC, sharing the one bound) from the budget's creation on,
    counting every request they send (in any context, token POSTs included).
    Enforced only while ``active()`` in the caller's context: there the request that
    would exceed it raises ``RequestBudgetExhausted`` before any I/O, and ``refused``
    counts those refusals. A nested ``active()`` (this budget or another) applies
    inside its block only; the outer one applies again after it."""

    def __init__(self, transports: "GuardedTransport | Iterable[GuardedTransport]",
                 limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("request budget must be an int >= 0")
        held = (transports,) if isinstance(transports, GuardedTransport) \
            else tuple(dict.fromkeys(transports))
        if not held or not all(isinstance(t, GuardedTransport) for t in held):
            raise ValueError("request budget needs one or more GuardedTransports")
        self.transports, self.limit = held, limit
        self._start = self._sent()
        self.refused = 0

    def _sent(self) -> int:
        return sum(t.requests_sent for t in self.transports)

    @property
    def used(self) -> int:
        return self._sent() - self._start

    @contextmanager
    def active(self) -> Iterator["RequestBudget"]:
        token = _ACTIVE_BUDGET.set(self)
        try:
            yield self
        finally:
            _ACTIVE_BUDGET.reset(token)

    def _admit(self, transport: "GuardedTransport") -> None:
        if any(t is transport for t in self.transports) and self.used >= self.limit:
            self.refused += 1
            raise RequestBudgetExhausted()


_ACTIVE_BUDGET: ContextVar[RequestBudget | None] = ContextVar("plx_request_budget",
                                                              default=None)


class RequestDeadlinePassed(RequestBudgetExhausted):
    """A request refused before I/O: an active ``RequestDeadline`` has (nearly) passed."""

    def __init__(self) -> None:
        SourceUnavailable.__init__(self, "request deadline passed")


class RequestDeadline:
    """A wall-clock bound on every request any ``GuardedTransport`` sends in the caller's
    context while ``active()``: a request with less than ``MIN_SECONDS`` left is refused
    before I/O (``RequestDeadlinePassed``, counted in ``refused``), and one that is sent
    waits at most the time left (its transport's timeout, never more, bounds each socket
    operation, so a connect or a silent server cannot outlast the deadline by more than
    one read of an answer already arriving). Unlike ``RequestBudget``, deadlines nest by
    intersection: every active one applies, so an inner one never lifts an outer one.
    Independent of the request-count budgets, which still apply."""

    MIN_SECONDS = 1.0

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic
                 ) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("request deadline must be a number of seconds >= 0")
        self.clock = clock
        self.at = clock() + float(seconds)
        self.refused = 0

    def left(self) -> float:
        return self.at - self.clock()

    @property
    def passed(self) -> bool:
        return self.left() < self.MIN_SECONDS

    @contextmanager
    def active(self) -> Iterator["RequestDeadline"]:
        token = _ACTIVE_DEADLINES.set(_ACTIVE_DEADLINES.get() + (self,))
        try:
            yield self
        finally:
            _ACTIVE_DEADLINES.reset(token)


_ACTIVE_DEADLINES: ContextVar[tuple[RequestDeadline, ...]] = ContextVar(
    "plx_request_deadlines", default=())


def _deadline_timeout(timeout: float) -> float:
    """``timeout`` cut to the time the active deadlines leave; raises
    ``RequestDeadlinePassed`` (before any I/O) when one leaves too little."""
    for deadline in _ACTIVE_DEADLINES.get():
        left = deadline.left()
        if left < deadline.MIN_SECONDS:
            deadline.refused += 1
            raise RequestDeadlinePassed()
        timeout = min(timeout, left)
    return timeout


class NotVisible(PermissionError):
    """The actor may not read this resource or item (or it does not exist)."""

    def __init__(self, code: str = "not_visible") -> None:
        super().__init__(code)
        self.code = code


def transport_failure(message: str) -> SourceUnavailable:
    """``SourceUnavailable`` for a sender failure before any HTTP status
    (``URLError``, timeout, connection reset/refused, TLS), marked
    ``transport_failure = True``. Transient, so an idempotent GET may be retried
    (``SourceApi`` with a ``BoundedBackoff``); other unavailability (bad
    response, size bound, invalid JSON) is not. The class stays
    ``SourceUnavailable`` so error labels downstream do not change."""
    error = SourceUnavailable(message)
    error.transport_failure = True
    return error


def is_transport_failure(error: BaseException) -> bool:
    return (isinstance(error, DownloadIncomplete)
            or (isinstance(error, SourceUnavailable)
                and getattr(error, "transport_failure", False) is True))


def nextlink_refused(message: str) -> SourceUnavailable:
    """``SourceUnavailable`` for a continuation refused before any I/O: a server
    ``@odata.nextLink`` (or a caller's stored cursor) that is not the same
    collection and query (``_same_collection_next``), marked ``nextlink_refused =
    True``. Never retried: the same link would be refused again, so a resumable
    reader drops it and starts the traversal over. The class stays
    ``SourceUnavailable`` so error labels downstream do not change."""
    error = SourceUnavailable(message)
    error.nextlink_refused = True
    return error


def is_nextlink_refusal(error: BaseException) -> bool:
    return (isinstance(error, SourceUnavailable)
            and getattr(error, "nextlink_refused", False) is True)


# Sanitised diagnosis of a ``SourceUnavailable`` (directive e402518f64be): a
# token from a FIXED vocabulary, derived from the messages this module and the
# readers raise. Never free text, URLs, ids, names or numbers from the source;
# an unknown message reads ``unclassified``. Callers (``operating.ingestion``)
# append it to the error class.
HTTP_STATUS_DETAILS = frozenset({400, 401, 403, 404, 405, 406, 408, 409, 410, 412, 413,
                                 416, 422, 423, 429, 500, 501, 502, 503, 504, 507, 509})
_TRANSPORT_DETAILS = {
    "timeout": "timeout", "timeouterror": "timeout", "urlerror": "urlerror",
    "connectionreseterror": "conn_reset", "connectionrefusederror": "conn_refused",
    "connectionabortederror": "conn_aborted", "remotedisconnected": "remote_closed",
    "sslerror": "tls_error", "sslcertverificationerror": "tls_error",
    "gaierror": "dns_error", "brokenpipeerror": "conn_reset",
}
_UNAVAILABLE_MESSAGES = (
    (re.compile(r"nextLink escaped the collection"), "nextlink_escaped"),
    (re.compile(r"pagination loop"), "pagination_loop"),
    (re.compile(r"listing lacks value list|listing item invalid"), "listing_invalid"),
    (re.compile(r"returned non-JSON"), "non_json"),
    (re.compile(r"download length header invalid"), "download_length_invalid"),
    (re.compile(r"download incomplete"), "download_incomplete"),
    (re.compile(r"response exceeds the \d+-byte bound|exceeds configured bound"),
     "response_too_large"),
    (re.compile(r"sender returned an invalid response"), "invalid_response"),
    (re.compile(r"token request refused"), "token_refused"),
    (re.compile(r"token response invalid"), "token_invalid"),
    (re.compile(r"missing environment variables"), "missing_env"),
    (re.compile(r"credential variables invalid|BC_ENVIRONMENT missing or invalid"),
     "credentials_invalid"),
    (re.compile(r"returned another identity"), "identity_mismatch"),
    (re.compile(r"lacks a valid id|lacks id|lacks @odata\.etag"), "record_invalid"),
    (re.compile(r"changed during content read"), "item_changed"),
    (re.compile(r"differ from|differ from item size"), "content_mismatch"),
    (re.compile(r"redirected"), "redirect_refused"),
    (re.compile(r"request budget exhausted"), "request_budget"),
    (re.compile(r"wall cap reached"), "wall_cap"),
)
_HTTP_STATUS = re.compile(r"(?:GET|download) returned HTTP (\d{3})\b")
_TRANSPORT_FAILED = re.compile(r" failed: ([A-Za-z][A-Za-z0-9_]{0,63})")
_GAVE_UP = re.compile(r"\(gave up after \d+ attempts\)")
# Prefix for a failure that outlived the bounded retry.
RETRIED_PREFIX = "retried."
# Every token ``unavailable_detail`` can return (without ``RETRIED_PREFIX``).
# Tokens stay <= 24 characters so ``source_error:<Class>:retried.<token>`` fits
# the live verifier's 64-character enum bound for every SourceUnavailable class
# (asserted by integrations.tests.test_live_source_guard_diag).
DETAIL_TOKENS = frozenset(
    {f"http_{code}" for code in HTTP_STATUS_DETAILS}
    | {"http_other", "unclassified", "download_incomplete", "download_length_invalid",
       "transport_failure", "request_failed"}
    | set(_TRANSPORT_DETAILS.values())
    | {token for _pattern, token in _UNAVAILABLE_MESSAGES})
MAX_DETAIL_TOKEN = 24


def unavailable_detail(exc: BaseException) -> str | None:
    """The fixed-vocabulary diagnosis of a source failure, or None when ``exc``
    is not a ``SourceUnavailable``. Shape: ``<token>`` or
    ``retried.<token>``; every token is in ``DETAIL_TOKENS``."""
    if not isinstance(exc, SourceUnavailable):
        return None
    message = str(exc)
    detail = "unclassified"
    status = _HTTP_STATUS.search(message)
    failed = _TRANSPORT_FAILED.search(message)
    if status is not None:
        code = int(status.group(1))
        detail = f"http_{code}" if code in HTTP_STATUS_DETAILS else "http_other"
    elif isinstance(exc, DownloadIncomplete):
        detail = ("download_length_invalid" if "length header" in message
                  else "download_incomplete")
    elif failed is not None:
        detail = _TRANSPORT_DETAILS.get(failed.group(1).lower(),
                                        "transport_failure" if is_transport_failure(exc)
                                        else "request_failed")
    else:
        for pattern, token in _UNAVAILABLE_MESSAGES:
            if pattern.search(message):
                detail = token
                break
    if _GAVE_UP.search(message):
        detail = RETRIED_PREFIX + detail
    return detail


# Upstream error-code class (directive 8f867fce7ba1): the ``error.code`` of a
# Graph/BC JSON error body, or the OAuth token endpoint's ``error`` string, as
# one token of a FIXED vocabulary (camelCase -> snake_case). A code outside the
# list reads ``other``: a code is source text and could carry a value. Never the
# message, the description or any other part of the body. One family is admitted by
# shape (gap-1b49174b629f, the payment journals' 400 read ``other`` with every code
# below listed): BC's platform codes ``<Category>_<Name>`` (``_BC_CODE``), a category of
# BC's own and a name of letters only, so no number, id, date, space or quote passes.
_UPSTREAM_ERROR_CODES = (
    # Microsoft Graph (drive, mail, directory) top-level codes.
    "accessDenied", "activityLimitReached", "extensionError", "generalException",
    "invalidRange", "invalidRequest", "itemNotFound", "malwareDetected",
    "nameAlreadyExists", "notAllowed", "notSupported", "resourceModified",
    "resyncRequired", "serviceNotAvailable", "syncStateNotFound", "quotaLimitReached",
    "unauthenticated", "InvalidAuthenticationToken", "Authorization_RequestDenied",
    "Authorization_IdentityNotFound", "Request_ResourceNotFound", "Request_BadRequest",
    "Request_ThrottledTemporarily", "ResourceNotFound", "ErrorAccessDenied",
    "ErrorItemNotFound", "ErrorInvalidIdMalformed", "ErrorInvalidUser",
    "MailboxNotEnabledForRESTAPI", "BadRequest", "Unauthorized", "Forbidden", "NotFound",
    "TooManyRequests", "ServiceUnavailable", "InternalServerError", "UnknownError",
    # Business Central API v2.0.
    "BadRequest_NotFound", "BadRequest_InvalidToken", "BadRequest_MethodNotAllowed",
    "Authentication_InvalidCredentials", "Internal_ServerError", "Request_EntityChanged",
    # Business Central platform codes an AL runtime error answers with (NavErrorCode):
    # a page trigger that fails (e.g. a FindFirst on a missing journal template).
    "BadRequest_ResourceNotFound", "Internal_RecordNotFound", "Internal_CompanyNotFound",
    "Internal_EntityWithSameKeyExists", "Application_DialogException",
    "Application_RecordNotFound", "Application_FieldValidationException",
    "Application_EntityValidationException", "Application_ObjectNotFound",
    "Application_InvalidFilter",
    # OAuth 2.0 token endpoint (RFC 6749 section 5.2 and Entra additions).
    "invalid_client", "invalid_grant", "invalid_request", "invalid_scope",
    "unauthorized_client", "unsupported_grant_type", "temporarily_unavailable",
    "interaction_required", "invalid_resource",
)


def _code_token(code: str) -> str:
    snake = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", code).lower()
    return re.sub(r"_+", "_", snake)


ERROR_CODE_TOKENS = frozenset({_code_token(code) for code in _UPSTREAM_ERROR_CODES}
                              | {"other"})
_BC_CATEGORIES = ("Application", "Authentication", "Authorization", "BadRequest", "Internal",
                  "Request")
_BC_CODE = re.compile(rf"(?:{'|'.join(_BC_CATEGORIES)})_[A-Z][A-Za-z]{{1,47}}")
_BC_CODE_TOKEN = re.compile(
    rf"(?:{'|'.join(_code_token(c) for c in _BC_CATEGORIES)})(?:_[a-z]+)+")
MAX_ERROR_CODE_TOKEN = 64


def is_error_code_token(value: Any) -> bool:
    """Whether ``value`` is an error-code token a failure may carry: one of
    ``ERROR_CODE_TOKENS`` or a token of BC's platform code family."""
    return isinstance(value, str) and (value in ERROR_CODE_TOKENS or (
        len(value) <= MAX_ERROR_CODE_TOKEN and _BC_CODE_TOKEN.fullmatch(value) is not None))


def upstream_error_code(raw: Any) -> str | None:
    """The fixed-vocabulary class of a JSON error body's code, ``other`` for a code
    outside it, None when the body carries no code (or is not JSON)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 65_536:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else error
    if not isinstance(code, str) or not code:
        return None
    token = _code_token(code) if len(code) <= 64 else ""
    if token in ERROR_CODE_TOKENS:
        return token
    return token if _BC_CODE.fullmatch(code) and is_error_code_token(token) else "other"


# Upstream message class (gap-1b49174b629f): what a JSON error body's ``error.message``
# is about, as one token of a FIXED vocabulary matched by fixed patterns, never a copy:
# the message is source text and carries names, numbers and filter values. First match
# wins; ``unclassified`` is a message no pattern names. The class is diagnosis only, never
# a verdict: the patterns are English guesses no live message has confirmed, so whether a
# refusal is expected is spec data (``bc_read.expected_refusal``), never this class
# (architect-20261013T0921Z). ``_MESSAGE_CLASSES`` name the journal setup a payment journal
# listing needs: its template, batch, number series or journal/payment setup (a setup
# table of another subject, e.g. a currency's, is no such class). The subject is the thing
# missing, not one named beside it ("The Gen. Journal Batch does not exist. ... Journal
# Template Name='X'" is a missing batch): the failure follows the subject within its
# clause, or "there is no" / "cannot find" precedes it.
_NOT_SET_UP = (r"(does not exist|must have a value|cannot be found|could not be found"
               r"|was not found|is missing|is mandatory|is not set up)")
_THERE_IS_NO = r"(there is no|cannot find|could not find|you must (specify|select|enter))"
_MESSAGE_CLASSES = (
    ("journal_template_missing", r"journal templ(ate|\.)"),
    ("journal_batch_missing", r"journal batch"),
    ("number_series_missing", r"(\bno\. series|number series)"),
    ("setup_missing", r"\b(journal|payments?)( registration)? setup"),
)
# A message naming a permission is permission_denied, whatever else it says.
_MESSAGE_PATTERNS = ((
    "permission_denied", re.compile(r"permission|not authori[sz]ed|access (is )?denied",
                                    re.IGNORECASE)),) + tuple(
    (token, re.compile(rf"{subject}(?!\w)[^.:'=]{{0,40}}?{_NOT_SET_UP}"
                       rf"|{_THERE_IS_NO}\s+(an?\s+|the\s+)?([\w.]+\s+){{0,2}}{subject}",
                       re.IGNORECASE))
    for token, subject in _MESSAGE_CLASSES) + tuple(
    (token, re.compile(pattern, re.IGNORECASE)) for token, pattern in (
        ("invalid_query", r"\$(select|filter|expand|orderby|top|skip)\b"
                          r"|could not find a property|query option|syntax error"),
        ("record_not_found", rf"{_NOT_SET_UP}|{_THERE_IS_NO}|within the filter"),
        ("field_validation", r"must be|must not be|is not valid|invalid value|not allowed"),
    ))
MESSAGE_CLASS_TOKENS = frozenset({token for token, _p in _MESSAGE_PATTERNS}
                                 | {"unclassified"})


def upstream_message_class(raw: Any) -> str | None:
    """The ``MESSAGE_CLASS_TOKENS`` class of a JSON error body's ``error.message``,
    ``unclassified`` for a message no pattern names, None when the body carries no message
    (or is not JSON). Only the token leaves: never the message or a part of it."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 65_536:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return next((token for token, pattern in _MESSAGE_PATTERNS
                 if pattern.search(message[:2048])), "unclassified")


def _http_failure(error: SourceUnavailable, status: int, raw: Any) -> SourceUnavailable:
    error.http_status = status
    error.error_code = upstream_error_code(raw)
    error.message_class = upstream_message_class(raw)
    return error


def failure_diagnostics(exc: BaseException) -> dict[str, Any]:
    """What a probe may print about a failure: the exception class, the HTTP status,
    the error-code token (``is_error_code_token``) and the ``DETAIL_TOKENS`` detail.
    Never the message, a URL, an id or any part of a body."""
    status = getattr(exc, "http_status", None)
    code = getattr(exc, "error_code", None)
    return {"error_class": type(exc).__name__,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "error_code": code if is_error_code_token(code) else None,
            "detail": unavailable_detail(exc)}


# Bounded retry for idempotent reads, shared by ``SourceApi`` (BC GETs) and
# ``plx_hub_guard.HubTransport`` (MC/brain GETs and read-only MCP calls).
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 4.0


class BoundedBackoff:
    """At most ``max_retries`` (<= 2) jittered exponential waits per call,
    each capped at ``BACKOFF_CAP_SECONDS``; ``Retry-After`` (delta-seconds or
    HTTP-date against the injected clock) raises a wait up to the cap, never
    beyond it. ``budget_seconds`` optionally bounds the total sleep across
    every call that shares this object (a long sequential burst then stops
    retrying instead of stalling). Sleep, jitter and clock are injected so
    tests never wait. It only computes and sleeps: callers decide which
    requests are idempotent (never a write, never the OAuth token POST)."""

    def __init__(self, *, max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = random.random,
                 clock: Callable[[], datetime] | None = None,
                 budget_seconds: float | None = None) -> None:
        if (isinstance(max_retries, bool) or not isinstance(max_retries, int)
                or not 0 <= max_retries <= MAX_RETRIES):
            raise ValueError(f"max_retries must be 0..{MAX_RETRIES}")
        if budget_seconds is not None and (isinstance(budget_seconds, bool) or
                                           not isinstance(budget_seconds, (int, float))
                                           or budget_seconds < 0):
            raise ValueError("budget_seconds must be >= 0")
        self.max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.budget_seconds = None if budget_seconds is None else float(budget_seconds)
        self.slept_seconds = 0.0
        self.retries = 0
        # A Retry-After one thread was given holds the other threads sharing this backoff
        # (``hold``): (monotonic end, the thread that waits it out itself).
        self._hold_lock = threading.Lock()
        self._hold: tuple[float, int] | None = None

    def __repr__(self) -> str:
        return f"BoundedBackoff(max_retries={self.max_retries}, retries={self.retries})"

    def retry_after(self, value: Any) -> float | None:
        """Seconds from ``Retry-After`` (delta-seconds or HTTP-date), else None."""
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None or when.tzinfo is None:
            return None
        return max(0.0, (when - self._clock()).total_seconds())

    def delay(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
        delay *= 0.5 + 0.5 * max(0.0, min(1.0, self._jitter()))
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        wait = self.retry_after(lowered.get("retry-after"))
        if wait is not None:
            delay = max(delay, min(BACKOFF_CAP_SECONDS, wait))
        return delay

    def request(self) -> None:
        """Called before each GET's first attempt; a subclass resets per-request state."""

    def may_retry(self, attempt: int, headers: Mapping[str, str] | None = None) -> bool:
        """Whether a retry after 0-based ``attempt`` stays within both bounds."""
        if attempt >= self.max_retries:
            return False
        if self.budget_seconds is None:
            return True
        return self.slept_seconds + self.delay(attempt, headers) <= self.budget_seconds

    def pause(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        """Sleep one bounded, jittered backoff step and return its length. A step the
        source asked for (``Retry-After``) also holds every other thread sharing this
        backoff until it ends (``hold``), so concurrent readers honour it together."""
        delay = self.delay(attempt, headers)
        if self.budget_seconds is not None:
            delay = min(delay, max(0.0, self.budget_seconds - self.slept_seconds))
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        if self.retry_after(lowered.get("retry-after")) is not None:
            with self._hold_lock:
                end = time.monotonic() + delay
                if self._hold is None or self._hold[0] < end:
                    self._hold = (end, threading.get_ident())
        self.slept_seconds += delay
        self.retries += 1
        self._sleep(delay)
        return delay

    def hold(self) -> float:
        """Before a request: wait out a ``Retry-After`` another thread was given (at most
        ``BACKOFF_CAP_SECONDS``); the wait slept (0 when none is running)."""
        with self._hold_lock:
            held = self._hold
        if held is None or held[1] == threading.get_ident():
            return 0.0
        left = min(BACKOFF_CAP_SECONDS, held[0] - time.monotonic())
        if left <= 0:
            return 0.0
        self._sleep(left)
        return left


class Secret:
    """A credential value that never renders."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("secret value required")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):  # never pickled into stored state
        raise TypeError("Secret cannot be serialized")


# ------------------------------------------------------------------ guard

def _denied_tokens(path: str) -> set[str]:
    stripped = _GRAPH_PATH_ADDRESS.sub("root:", path)
    found = set()
    for segment in stripped.split("/"):
        decoded = unquote(segment).lower()
        if "microsoft.nav." in decoded:
            found.add("microsoft.nav")
        for token in _TOKEN_SPLIT.split(decoded):
            if token in DENIED_PATH_TOKENS:
                found.add(token)
    return found


def _teams_urls(environ: Mapping[str, str]) -> list[str]:
    # Only these names' values are read: a request never reads another source's secret.
    values = [environ[key] for key in list(environ)
              if key.upper().startswith("MC_GO_LIVE_TEAMS_")]
    return [value for value in values if isinstance(value, str) and value.strip()]


def check_request(method: str, url: str, *, body: Mapping[str, str] | None = None,
                  environ: Mapping[str, str] | None = None) -> str:
    """Validate one credentialed request; return its host. Raise before I/O."""
    if not isinstance(method, str) or not isinstance(url, str):
        raise ReadOnlyGuardError("request method/url invalid")
    method = method.upper()
    if len(url) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ReadOnlyGuardError("request url invalid")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ReadOnlyGuardError("request url invalid") from None
    if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
            or port is not None or parts.netloc.lower() != host):
        raise ReadOnlyGuardError("request url must be plain https without port or userinfo")
    if host not in ALLOWED_HOSTS:
        raise ReadOnlyGuardError("host not in read-only allowlist")
    for teams in _teams_urls(os.environ if environ is None else environ):
        teams_host = (urlsplit(teams.strip()).hostname or "").lower()
        if teams.strip() in url or (teams_host and teams_host == host):
            raise ReadOnlyGuardError("MC_GO_LIVE_TEAMS_* URLs are never called")
    if method == "GET":
        if body is not None:
            raise ReadOnlyGuardError("GET carries no body")
        if host in TOKEN_HOSTS:
            raise ReadOnlyGuardError("login host accepts only the token POST")
        path = parts.path
        if "\\" in path or not grammar.segments_clean(path):
            raise ReadOnlyGuardError("path segment carries '/', '\\', '%', '..' after decoding")
        if grammar.match(host, path) is None:
            raise ReadOnlyGuardError("path is not an operation of the closed request grammar")
        if _denied_tokens(path):
            raise ReadOnlyGuardError("action-like path segment refused")
        if host == BC_HOST and not grammar.bc_request_query_ok(path, parts.query):
            # BC queries (incl. nextLinks): only what grammar.bc_query builds.
            raise ReadOnlyGuardError("BC query is not one the closed grammar builds")
        if host in grammar.EXTERNAL_HOSTS and not grammar.external_query_ok(
                grammar.match(host, path), parts.query):
            raise ReadOnlyGuardError("query is not one the closed external grammar allows")
        return host
    if method == "POST" and host == LWA_HOST:
        # Login with Amazon: the one refresh-token exchange for an SP-API access token.
        if parts.path != LWA_TOKEN_PATH or parts.query:
            raise ReadOnlyGuardError("only the OAuth token POST is permitted")
        if (not isinstance(body, Mapping) or body.get("grant_type") != "refresh_token"
                or set(body) != LWA_FORM_KEYS
                or not all(isinstance(v, str) and v for v in body.values())):
            raise ReadOnlyGuardError("token POST must be a refresh-token request")
        return host
    if method == "POST":
        match = _TOKEN_PATH.fullmatch(parts.path)
        if host != LOGIN_HOST or match is None or parts.query:
            raise ReadOnlyGuardError("only the OAuth token POST is permitted")
        if _TENANT.fullmatch(match[1]) is None:
            raise ReadOnlyGuardError("token tenant invalid")
        if (not isinstance(body, Mapping) or body.get("grant_type") != "client_credentials"
                or body.get("scope") not in ALLOWED_SCOPES
                or set(body) != {"grant_type", "scope", "client_id", "client_secret"}):
            raise ReadOnlyGuardError("token POST must be a client-credentials request")
        return host
    raise ReadOnlyGuardError(f"method {method} refused; read-only")


def sharepoint_host_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """The one download host: the host part of a composite ``SHAREPOINT_SITE_ID``."""
    env = os.environ if environ is None else environ
    site_host = (env.get("SHAREPOINT_SITE_ID") or "").split(",", 1)[0].strip().lower()
    return site_host if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com",
                                     site_host) else None


def tenant_download_hosts(host: str | None) -> frozenset[str]:
    """The configured SharePoint host and its tenant's OneDrive (``-my``) host: the one
    derivation of the tenant's hosts (the guard's download hosts and the SharePoint
    estate discovery's hosts, ``capture_sharepoint.estate_hosts``). None or empty: none."""
    if not host:
        return frozenset()
    host = host.lower()
    tenant = host.split(".", 1)[0].removesuffix("-my")
    return frozenset({host, f"{tenant}-my.sharepoint.com"})


Sender = Callable[[str, str, Mapping[str, str], "bytes | None", float, int],
                  "tuple[int, Mapping[str, str], bytes]"]


CONTENT_OPERATIONS = frozenset({"graph.drive_item_content", "graph.drive_item_version_content"})


def _content_redirect(url: str, status: int = 302) -> bool:
    """A 302 answering a content GET (``CONTENT_OPERATIONS``: an item's current bytes or
    one named version's): handed back, never followed."""
    parts = urlsplit(url)
    return (status == 302 and (parts.hostname or "").lower() == GRAPH_HOST
            and grammar.match(GRAPH_HOST, parts.path) in CONTENT_OPERATIONS)


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req is not None and _content_redirect(req.full_url, code):
            return None  # not followed; urllib_sender hands the 302 back
        target = (urlsplit(newurl).hostname or "").lower()
        if target not in ALLOWED_HOSTS:
            raise ReadOnlyGuardError("redirect to a host outside the allowlist refused")
        raise ReadOnlyGuardError("redirect refused; credentials are never forwarded")


# A connection that is not up (TCP connect, proxy CONNECT, TLS handshake) within this
# fails at once; the request's own timeout still bounds each read of the answer. Live BC
# GETs that failed before any status took about 16 s per attempt under the one 30 s
# timeout (devloop/DECISIONS/2026-10-08-bc-three-way-connect-timeout.md).
CONNECT_TIMEOUT_SECONDS = 10.0

# Keep-alive pool for GETs (gap-e7f87c40ec57: a SharePoint capture spent about a third of
# its time on one TCP+TLS handshake per request). Small and bounded: idle connections per
# (connect host, tunnel host, proxy credential) key and in total; an idle connection
# older than KEEPALIVE_IDLE_SECONDS is closed instead of reused.
KEEPALIVE_PER_HOST = 4
KEEPALIVE_TOTAL = 16
KEEPALIVE_IDLE_SECONDS = 20.0
# Failures of a *reused* connection before any status that mean the server had already
# closed it while idle: the GET (never another method) is sent once more on a new one.
_STALE_CONNECTION = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class _ConnectionPool:
    """Idle keep-alive connections. A connection is either idle here or leased by exactly
    one request (``take`` removes it under the lock), so two threads never share one."""

    def __init__(self, per_key: int = KEEPALIVE_PER_HOST, total: int = KEEPALIVE_TOTAL,
                 idle_seconds: float = KEEPALIVE_IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._per_key = per_key
        self._total = total
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._idle: dict[tuple, list[tuple[Any, float]]] = {}

    def take(self, key: tuple) -> Any | None:
        expired: list[Any] = []
        found = None
        with self._lock:
            idle = self._idle.get(key, [])
            now = self._clock()
            while idle:
                conn, since = idle.pop()
                if now - since <= self._idle_seconds and conn.sock is not None:
                    found = conn
                    break
                expired.append(conn)
            if not idle:
                self._idle.pop(key, None)
        for conn in expired:
            conn.close()
        return found

    def give(self, key: tuple, conn: Any) -> None:
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if (len(idle) < self._per_key
                    and sum(len(v) for v in self._idle.values()) < self._total):
                idle.append((conn, self._clock()))
                return
            if not idle:
                self._idle.pop(key, None)
        conn.close()

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._idle.values())

    def clear(self) -> None:
        with self._lock:
            conns = [conn for idle in self._idle.values() for conn, _since in idle]
            self._idle.clear()
        for conn in conns:
            conn.close()


_POOL = _ConnectionPool()
# The TLS context the handler builds by default (on Python 3.12+ urllib's own
# ``http.client`` default: ``ssl.create_default_context()``, CERT_REQUIRED, hostname
# checked, ALPN http/1.1), built once and shared instead of once per request. None
# (Python < 3.12): every new connection builds that same default itself. No setting of
# it is changed here.
_TLS_CONTEXT: list = []
_TLS_LOCK = threading.Lock()


def _shared_tls_context() -> Any:
    with _TLS_LOCK:
        if not _TLS_CONTEXT:
            _TLS_CONTEXT.append(HTTPSHandler()._context)
        return _TLS_CONTEXT[0]


class _BoundedConnect(HTTPSHandler):
    """HTTPS with the connect bounded apart from the read: a connection is opened with
    ``min(CONNECT_TIMEOUT_SECONDS, timeout)`` and its socket then waits ``timeout`` per
    read. Same connection class and TLS context as the default handler.

    A GET takes an idle keep-alive connection of the same key from the pool (else opens
    one) and holds it as this handler's lease; ``urllib_sender`` hands it back only after
    the response was read whole to its end (``release``) and ``discard`` closes it in
    every other case. Any other method opens its own connection and closes it after the
    one request (``Connection: close``), as before."""

    def __init__(self, pool: _ConnectionPool | None = None) -> None:
        super().__init__(context=_shared_tls_context())
        self._pool = _POOL if pool is None else pool
        self._lease: tuple[tuple, Any, Any] | None = None

    @staticmethod
    def _connection(http_class, host, timeout, **kwargs):
        conn = http_class(host, timeout=min(CONNECT_TIMEOUT_SECONDS, timeout), **kwargs)
        connect = conn.connect

        def bounded_connect():
            connect()
            conn.sock.settimeout(timeout)
        conn.connect = bounded_connect
        return conn

    def do_open(self, http_class, req, **http_conn_args):
        if req.get_method() != "GET":
            def connection(host, timeout=None, **kwargs):
                return self._connection(http_class, host, timeout, **kwargs)
            return super().do_open(connection, req, **http_conn_args)
        return self._open_pooled(http_class, req, http_conn_args)

    def _open_pooled(self, http_class, req, http_conn_args):
        # AbstractHTTPHandler.do_open without its forced ``Connection: close``.
        if self._lease is not None:
            raise ReadOnlyGuardError("sender: one request per handler")
        host = req.host
        if not host:
            raise URLError("no host given")
        headers = dict(req.unredirected_hdrs)
        headers.update(req.headers)
        headers = {name.title(): value for name, value in headers.items()}
        tunnel_headers: dict[str, str] = {}
        if req._tunnel_host and "Proxy-Authorization" in headers:
            tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")
        key = (http_class, urlsplit(req.full_url).scheme, host, req._tunnel_host,
               sha256(repr(sorted(tunnel_headers.items())).encode()).hexdigest())
        for attempt in range(2):
            conn = self._pool.take(key) if attempt == 0 else None
            reused = conn is not None
            if conn is None:
                conn = self._connection(http_class, host, req.timeout, **http_conn_args)
                conn.set_debuglevel(self._debuglevel)
                if req._tunnel_host:
                    conn.set_tunnel(req._tunnel_host, headers=tunnel_headers)
            else:
                conn.sock.settimeout(req.timeout)
            try:
                try:
                    conn.request("GET", req.selector, req.data, headers,
                                 encode_chunked=req.has_header("Transfer-encoding"))
                except OSError as err:
                    raise URLError(err)
                response = conn.getresponse()
            except BaseException as exc:
                conn.close()
                cause = exc.reason if isinstance(exc, URLError) else exc
                if reused and isinstance(cause, _STALE_CONNECTION):
                    continue
                raise
            break
        self._lease = (key, conn, response)
        response.url = req.get_full_url()
        response.msg = response.reason
        return response

    def release(self, response: Any) -> None:
        """Back to the pool only when ``response`` is this lease's and was read to its
        framed end (http.client closed it), and neither side asked to close."""
        lease, self._lease = self._lease, None
        if lease is None:
            return
        key, conn, leased = lease
        if (leased is response and response.isclosed() and not response.will_close
                and conn.sock is not None):
            self._pool.give(key, conn)
        else:
            conn.close()

    def discard(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease[1].close()


def _refuse_non_read_send(method: str, url: str, body: bytes | None) -> None:
    """Last-line check inside the sender, independent of its callers: only a plain
    https GET without a body, the exact OAuth client-credentials token POST or the exact
    Login with Amazon refresh-token POST (hosts are ``check_request``'s, before this)."""
    if not isinstance(method, str) or not isinstance(url, str):
        raise ReadOnlyGuardError("sender: method/url invalid")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ReadOnlyGuardError("sender: url invalid") from None
    if (parts.scheme != "https" or not host or port is not None or parts.username
            or parts.password or parts.fragment):
        raise ReadOnlyGuardError("sender: plain https url required")
    if method == "GET":
        if body is not None:
            raise ReadOnlyGuardError("sender: GET carries no body")
        return
    if method == "POST" and host == LOGIN_HOST and not parts.query:
        match = _TOKEN_PATH.fullmatch(parts.path)
        try:
            form = parse_qs((body or b"").decode("ascii"), keep_blank_values=True,
                            strict_parsing=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            form = {}
        if (match is not None and _TENANT.fullmatch(match[1]) is not None
                and set(form) == {"grant_type", "scope", "client_id", "client_secret"}
                and all(len(v) == 1 for v in form.values())
                and form["grant_type"] == ["client_credentials"]
                and form["scope"][0] in ALLOWED_SCOPES):
            return
    if method == "POST" and host == LWA_HOST and parts.path == LWA_TOKEN_PATH \
            and not parts.query:
        try:
            form = parse_qs((body or b"").decode("ascii"), keep_blank_values=False,
                            strict_parsing=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            form = {}
        if (set(form) == LWA_FORM_KEYS and all(len(v) == 1 for v in form.values())
                and form["grant_type"] == ["refresh_token"]):
            return
    if method == "POST" and not parts.query:
        # Mission Control JSON-RPC: plx_hub_guard.check_mc_request has already limited
        # the tools and any write authorization; here only the shape is re-checked.
        from .plx_hub_guard import MC_HOSTS, MCP_PATH
        try:
            rpc = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            rpc = None
        if (host in MC_HOSTS and parts.path == MCP_PATH and isinstance(rpc, dict)
                and rpc.get("jsonrpc") == "2.0" and isinstance(rpc.get("method"), str)):
            return
    raise ReadOnlyGuardError("sender: only GET, the OAuth token POST or the MC JSON-RPC "
                             "POST may be sent")


def urllib_sender(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                  timeout: float, max_bytes: int) -> tuple[int, Mapping[str, str], bytes]:
    """Real network sender; called only by ``GuardedTransport`` after checks."""
    _refuse_non_read_send(method, url, body)
    request = Request(url, data=body, headers=dict(headers), method=method)
    connect = _BoundedConnect()
    try:
        try:
            opener = build_opener(_RefuseRedirect(), connect)
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            status = error.code
            if _content_redirect(url, status):
                location = error.headers.get("Location") if error.headers is not None else None
                return status, ({"location": location} if isinstance(location, str) else {}), b""
            if 300 <= status < 400:
                raise ReadOnlyGuardError("redirect refused") from None
            raw = error.read(65_536) if hasattr(error, "read") else b""
            return status, _error_headers(getattr(error, "headers", None)), raw
        with response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            raw = _read_to_eof(response, max_bytes + 1)
            _require_declared_length(response_headers, raw, max_bytes)
            connect.release(response)
            return getattr(response, "status", 200), response_headers, raw
    finally:
        # Any path that did not release the connection (error status, redirect, partial
        # or oversized read, timeout, exception) closes it; it is never pooled.
        connect.discard()


# Header names an error response may hand back to the caller (lower-cased):
# only what ``BoundedBackoff`` reads. Nothing else of an error response leaves
# the sender.
ERROR_RESPONSE_HEADERS = frozenset({"retry-after"})


def _error_headers(headers: Any) -> dict[str, str]:
    """The allowed headers of an ``HTTPError`` (so ``Retry-After`` on a
    429/503 is honoured on the real network path), lower-cased; ``{}`` when
    the error carries none or they are unreadable."""
    try:
        items = list(headers.items()) if headers is not None else []
    except Exception:  # noqa: BLE001 - a malformed header block is simply absent
        return {}
    kept: dict[str, str] = {}
    for key, value in items:
        name = str(key).lower()
        if name in ERROR_RESPONSE_HEADERS and isinstance(value, str) and len(value) <= 64:
            kept[name] = value
    return kept


_READ_CHUNK_BYTES = 65_536


def _read_to_eof(response: Any, limit: int) -> bytes:
    """Read until EOF, never more than ``limit`` bytes in total.

    A single ``read(n)`` may legitimately return fewer than ``n`` bytes, and
    ``http.client`` returns a short body without raising when the connection
    ends early, so one read is never proof of a whole body.
    """
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        try:
            chunk = response.read(min(_READ_CHUNK_BYTES, limit - total))
        except IncompleteRead:
            raise DownloadIncomplete() from None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _require_declared_length(headers: Mapping[str, str], raw: bytes, max_bytes: int) -> None:
    """Refuse a body shorter than its declared ``Content-Length``.

    Bodies over ``max_bytes`` are left to the transport's bound check. urllib
    does not decode ``Content-Encoding``, so the header counts the bytes read.
    """
    declared = headers.get("content-length")
    if declared is None or len(raw) > max_bytes:
        return
    try:
        expected = int(str(declared).strip())
    except ValueError:
        raise DownloadIncomplete("download length header invalid") from None
    if expected < 0:
        raise DownloadIncomplete("download length header invalid")
    if len(raw) != expected:
        raise DownloadIncomplete()


class GuardedTransport:
    """The one I/O entry point for live Microsoft sources (see module docstring)."""

    MAX_SECRETS = 16

    def __init__(self, sender: Sender = urllib_sender, *,
                 environ: Mapping[str, str] | None = None,
                 timeout_seconds: float = 30.0,
                 sharepoint_host: str | None = None) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout must be 0..60 seconds")
        if sharepoint_host is not None:
            host = sharepoint_host.lower()
            if (re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.sharepoint\.com", host) is None):
                raise ValueError("sharepoint host must be <tenant>.sharepoint.com")
            sharepoint_host = host
        self.sharepoint_host = sharepoint_host
        self._sender = sender
        # Evidence labelling only (never a control): a test sender is injected-transport.
        self.sender_injected = sender is not urllib_sender
        self._environ = environ
        self._timeout = float(timeout_seconds)
        self._secrets: list[Secret] = []
        # Every request sent, and the GETs among them (never the OAuth token POST).
        self._count_lock = threading.Lock()
        self.requests_sent = 0
        self.gets_sent = 0
        # The grammar operations of the GETs a source answered (status < 400): what a
        # live check's result names as read (scripts/source_registry.py, Graph endpoints).
        self.operations_read: set[str] = set()
        # Request observers, called with (method, url) before every request is sent
        # (never a control: an observer that raises stops that request). The URL may be
        # a pre-authenticated download URL: an observer keeps nothing of it beyond the
        # process and prints none of it (integrations.mail_directory_scope.RequestLog).
        self.observers: list[Callable[[str, str], None]] = []

    def __repr__(self) -> str:
        return f"GuardedTransport(requests_sent={self.requests_sent})"

    def register_secret(self, secret: Secret) -> Secret:
        """Bounded: long-lived secrets (client secret, current token) plus the
        call-scoped download URL; callers ``forget_secret`` what they scoped."""
        if any(s is secret for s in self._secrets):
            return secret
        if len(self._secrets) >= self.MAX_SECRETS:
            raise RuntimeError("secret registry bound exceeded")
        self._secrets.append(secret)
        return secret

    def forget_secret(self, secret: Secret | None) -> None:
        self._secrets = [s for s in self._secrets if s is not secret]

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            value = secret.reveal()
            if value and value in text:
                text = text.replace(value, "<redacted>")
        return text

    def _send(self, method: str, url: str, headers: Mapping[str, str],
              body: bytes | None, max_bytes: int) -> tuple[int, Mapping[str, str], bytes]:
        budget = _ACTIVE_BUDGET.get()
        if budget is not None:
            budget._admit(self)
        timeout = _deadline_timeout(self._timeout)
        for observe in self.observers:
            observe(method, url)
        with self._count_lock:   # concurrent readers share one transport
            self.requests_sent += 1
            if method == "GET":
                self.gets_sent += 1
        try:
            status, response_headers, raw = self._sender(method, url, headers, body,
                                                         timeout, max_bytes)
        except ReadOnlyGuardError:
            raise
        except DownloadIncomplete as exc:
            # A fixed message from this module; never the URL or the body.
            raise DownloadIncomplete(self.scrub(str(exc))) from None
        except OSError as exc:
            # URLError, timeouts, resets (incl. http.client.RemoteDisconnected), TLS:
            # no status arrived (retryable for a GET).
            raise transport_failure(self.scrub(
                f"{method} to {urlsplit(url).hostname} failed: {type(exc).__name__}")) from None
        except Exception as exc:
            raise SourceUnavailable(self.scrub(
                f"{method} to {urlsplit(url).hostname} failed: {type(exc).__name__}")) from None
        if not isinstance(status, int) or not isinstance(raw, (bytes, bytearray)):
            raise SourceUnavailable("sender returned an invalid response")
        if 300 <= status < 400 and not _content_redirect(url, status):
            raise ReadOnlyGuardError("redirect refused; credentials are never forwarded")
        if len(raw) > max_bytes:
            raise SourceUnavailable(f"response exceeds the {max_bytes}-byte bound")
        return status, dict(response_headers or {}), bytes(raw)

    def request(self, method: str, url: str, headers: Mapping[str, str], *,
                form: Mapping[str, str] | None = None,
                max_bytes: int = 4_000_000) -> tuple[int, Mapping[str, str], bytes]:
        host = check_request(method, url, body=form, environ=self._environ)
        op = grammar.match(host, urlsplit(url).path) if method.upper() == "GET" else None
        limit = (MAX_ATTACHMENT_RESPONSE_BYTES if op in _LARGE_RESPONSE_OPERATIONS
                 else MAX_RESPONSE_BYTES)
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= limit:
            raise ValueError("response bound invalid")
        body = urlencode(dict(form)).encode() if form is not None else None
        sent_headers = dict(headers)
        if body is not None:
            sent_headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, response_headers, raw = self._send(method.upper(), url, sent_headers, body,
                                                   max_bytes)
        if op is not None and status < 400:
            self.operations_read.add(op)
        return status, response_headers, raw

    @property
    def download_hosts(self) -> frozenset[str]:
        """The configured SharePoint host and its tenant's OneDrive (``-my``) host."""
        return tenant_download_hosts(self.sharepoint_host)

    def check_download_url(self, url: Any) -> str:
        """Refuse (before I/O) any download URL not on the tenant's download hosts."""
        allowed = self.download_hosts
        if not allowed:
            raise ReadOnlyGuardError("no SharePoint host configured; downloads refused")
        if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
            raise ReadOnlyGuardError("download url invalid")
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:
            raise ReadOnlyGuardError("download url invalid") from None
        if (parts.scheme != "https" or parts.username or parts.password or port is not None
                or parts.fragment or host not in allowed or parts.netloc.lower() != host):
            raise ReadOnlyGuardError("download url outside the configured SharePoint host")
        for teams in _teams_urls(os.environ if self._environ is None else self._environ):
            if teams.strip() in url:
                raise ReadOnlyGuardError("MC_GO_LIVE_TEAMS_* URLs are never called")
        return url

    def download_preauthenticated(self, url: str, *, max_bytes: int) -> bytes:
        """Credential-free GET of a pre-authenticated download URL on the one
        configured tenant SharePoint host (``sharepoint_host``)."""
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_CONTENT_BYTES:
            raise ValueError("content bound invalid")
        self.check_download_url(url)
        status, _headers, raw = self._send("GET", url, {"Accept": "application/octet-stream"},
                                           None, max_bytes)
        if status != 200:
            raise _http_failure(SourceUnavailable(f"download returned HTTP {status}"),
                                status, raw)
        return raw


# ------------------------------------------------------------------ credentials

@dataclass(frozen=True, repr=False)
class ClientCredentials:
    tenant_id: str
    client_id: str
    client_secret: Secret

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or _TENANT.fullmatch(self.tenant_id) is None:
            raise ValueError("tenant id invalid")
        if not isinstance(self.client_id, str) or not re.fullmatch(r"[0-9A-Za-z-]{8,64}",
                                                                   self.client_id):
            raise ValueError("client id invalid")
        if not isinstance(self.client_secret, Secret):
            raise ValueError("client secret must be a Secret")

    def __repr__(self) -> str:
        return "ClientCredentials(<redacted>)"

    @classmethod
    def from_env(cls, prefix: str, environ: Mapping[str, str] | None = None
                 ) -> "ClientCredentials":
        """``prefix`` is ``MICROSOFT_GRAPH`` or ``BC``; values are never echoed."""
        env = os.environ if environ is None else environ
        names = [f"{prefix}_TENANT_ID", f"{prefix}_CLIENT_ID", f"{prefix}_CLIENT_SECRET"]
        missing = [name for name in names if not env.get(name)]
        if missing:
            raise SourceUnavailable("missing environment variables: " + ", ".join(missing))
        try:
            return cls(env[names[0]].strip(), env[names[1]].strip(),
                       Secret(env[names[2]]))
        except ValueError as exc:
            raise SourceUnavailable(f"{prefix} credential variables invalid: {exc}") from None


class TokenProvider:
    """App-only client-credentials token for one scope, cached until near expiry."""

    def __init__(self, credentials: ClientCredentials, scope: str,
                 transport: GuardedTransport, *, clock: Callable[[], float] = time.time) -> None:
        if scope not in ALLOWED_SCOPES:
            raise ValueError("scope not allowed")
        self._credentials = credentials
        self._scope = scope
        self._transport = transport
        self._clock = clock
        self._token: Secret | None = None
        self._expires_at = 0.0
        # Concurrent readers share one provider: one token request at a time.
        self._lock = threading.Lock()
        transport.register_secret(credentials.client_secret)

    def __repr__(self) -> str:
        return f"TokenProvider(scope={self._scope!r})"

    def bearer(self) -> str:
        with self._lock:
            return self._bearer()

    def _bearer(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._expires_at - 120:
            return self._token.reveal()
        url = f"https://{LOGIN_HOST}/{self._credentials.tenant_id}/oauth2/v2.0/token"
        status, _headers, raw = self._transport.request("POST", url, {"Accept": "application/json"},
            form={"grant_type": "client_credentials", "scope": self._scope,
                  "client_id": self._credentials.client_id,
                  "client_secret": self._credentials.client_secret.reveal()},
            max_bytes=200_000)
        if status != 200:
            raise _http_failure(SourceUnavailable(f"token request refused: HTTP {status}"),
                                status, raw)
        try:
            payload = json.loads(raw)
            token = payload["access_token"]
            lifetime = int(payload.get("expires_in", 3599))
        except (ValueError, KeyError, TypeError):
            raise SourceUnavailable("token response invalid") from None
        if not isinstance(token, str) or not token:
            raise SourceUnavailable("token response invalid")
        self._transport.forget_secret(self._token)
        self._token = self._transport.register_secret(Secret(token))
        self._expires_at = now + max(0, min(lifetime, 86_400))
        return token


# ------------------------------------------------------------------ JSON API

def _decimal_json(raw: bytes, *, exact_integers: bool = False) -> Any:
    if exact_integers:
        return json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    return json.loads(raw, parse_float=Decimal)


def jsonable(value: Any) -> Any:
    """Decimal → exact text (money/quantities are never floats in records)."""
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def text_bytes(text: str, encoding: str = "utf-8") -> bytes:
    """``text`` in a UTF ``encoding``; a string that is no Unicode text (a lone surrogate:
    JSON accepts ``"\\ud800"``) is ``ValueError("text_not_unicode")``, never a codec error."""
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        raise ValueError("text_not_unicode") from None


def canonical_sha256(value: Any) -> str:
    return sha256(json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class SourceApi:
    """Authorized JSON GETs for one source through the guarded transport."""

    def __init__(self, source: str, tokens: TokenProvider, transport: GuardedTransport, *,
                 exact_integers: bool = False, retry: "BoundedBackoff | None" = None) -> None:
        if retry is not None and not isinstance(retry, BoundedBackoff):
            raise ValueError("retry must be a BoundedBackoff")
        self.source = source
        self._exact_integers = exact_integers
        self._tokens = tokens
        self._transport = transport
        self._retry = retry
        self.last_attempts = 0

    def __repr__(self) -> str:
        return f"SourceApi({self.source!r})"

    @property
    def retry(self) -> "BoundedBackoff | None":
        return self._retry

    def _send_get(self, url: str, sent: Mapping[str, str],
                  max_bytes: int) -> tuple[int, Mapping[str, str], bytes]:
        """One GET with the bounded retry (none without a ``BoundedBackoff``).

        Retried: a sender failure with no status (``is_transport_failure``:
        URLError, timeout, reset, TLS; or a body cut short) and HTTP
        429/502/503/504. Never retried: any other status (400/401/403/404 ...),
        a guard refusal, a size bound or a bad response. Only this GET is
        repeated; the bearer was fetched once before, so the OAuth token POST
        is never resent by this loop. After the bound the original error is
        raised (or the last status returned) with the attempt count."""
        if self._retry is not None:
            self._retry.hold()
            self._retry.request()
        attempt = 0
        while True:
            self.last_attempts = attempt + 1
            try:
                status, response_headers, raw = self._transport.request(
                    "GET", url, sent, max_bytes=max_bytes)
            except SourceUnavailable as exc:
                if not is_transport_failure(exc):
                    raise
                if self._retry is not None and self._retry.may_retry(attempt):
                    self._retry.pause(attempt)
                    attempt += 1
                    continue
                if attempt:
                    labelled = type(exc)(f"{exc} (gave up after {attempt + 1} attempts)")
                    labelled.transport_failure = True
                    raise labelled from None
                raise
            if (status in RETRY_STATUSES and self._retry is not None
                    and self._retry.may_retry(attempt, response_headers)):
                self._retry.pause(attempt, response_headers)
                attempt += 1
                continue
            return status, response_headers, raw

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None,
                 max_bytes: int = 4_000_000) -> Any:
        # Check before the token request so a refused URL causes no I/O at all.
        check_request("GET", url, environ=self._transport._environ)
        sent = {"Accept": "application/json", **(headers or {}),
                "Authorization": "Bearer " + self._tokens.bearer()}
        status, response_headers, raw = self._send_get(url, sent, max_bytes)
        tried = (f" (gave up after {self.last_attempts} attempts)"
                 if self.last_attempts > 1 else "")
        if status == 404:
            raise _http_failure(SourceNotFound(f"{self.source} GET returned HTTP 404"),
                                404, raw)
        if status != 200:
            # A caller that pauses a long read (``BcCapture``) honours the source's own
            # wait beyond the backoff cap: status and Retry-After seconds, never a value.
            error = _http_failure(
                SourceUnavailable(f"{self.source} GET returned HTTP {status}{tried}"),
                status, raw)
            lowered = {str(k).lower(): v for k, v in (response_headers or {}).items()}
            error.retry_after_seconds = (self._retry or BoundedBackoff()).retry_after(
                lowered.get("retry-after"))
            raise error
        try:
            return _decimal_json(raw, exact_integers=self._exact_integers)
        except ValueError:
            raise SourceUnavailable(f"{self.source} GET returned non-JSON") from None

    def get_document(self, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
        """The bytes of one schema document (``DOCUMENT_OPERATIONS``: BC ``$metadata``,
        CSDL XML), never a record: any other operation is refused before any I/O."""
        check_request("GET", url, environ=self._transport._environ)
        parts = urlsplit(url)
        if grammar.match((parts.hostname or "").lower(), parts.path) not in DOCUMENT_OPERATIONS:
            raise ReadOnlyGuardError("only a declared schema document is read as a document")
        sent = {"Accept": "application/xml",
                "Authorization": "Bearer " + self._tokens.bearer()}
        status, _headers, raw = self._send_get(url, sent, max_bytes)
        if status == 404:
            raise _http_failure(SourceNotFound(f"{self.source} GET returned HTTP 404"), 404, raw)
        if status != 200:
            raise _http_failure(SourceUnavailable(f"{self.source} GET returned HTTP {status}"),
                                status, raw)
        return raw

    def get_content(self, url: str, *, max_bytes: int) -> bytes:
        """Bytes of one drive item (or one named version of it) via a content route
        (``CONTENT_OPERATIONS``): the credentialed
        GET must answer 302; its Location (a pre-authenticated URL) is held as a
        ``Secret`` and fetched credential-free by ``download_preauthenticated``
        (tenant download hosts, ``max_bytes`` cap). Anything else fails closed."""
        check_request("GET", url, environ=self._transport._environ)
        if not _content_redirect(url):
            raise ReadOnlyGuardError("only a drive item content route answers with content")
        sent = {"Authorization": "Bearer " + self._tokens.bearer()}
        status, response_headers, raw = self._send_get(url, sent, 65_536)
        if status == 404:
            raise _http_failure(SourceNotFound(f"{self.source} GET returned HTTP 404"), 404, raw)
        if status != 302:
            raise _http_failure(SourceUnavailable(f"{self.source} GET returned HTTP {status}"),
                                status, raw)
        location = {str(k).lower(): v for k, v in response_headers.items()}.get("location")
        if not isinstance(location, str) or not location:
            raise SourceUnavailable(f"{self.source} content redirect lacks a location")
        secret = self._transport.register_secret(Secret(location))
        try:
            return self._transport.download_preauthenticated(secret.reveal(),
                                                             max_bytes=max_bytes)
        finally:
            self._transport.forget_secret(secret)

    def continuation_url(self, url: str, continuation: Any) -> str | None:
        """``_same_collection_next`` against this transport's environment: the URL
        to GET for ``continuation`` (a server nextLink or a stored cursor) after
        ``url``, or None when it is not the same collection and query or the guard
        would refuse it."""
        return _same_collection_next(url, continuation, environ=self._transport._environ)

    def get_page(self, url: str, *, headers: Mapping[str, str] | None = None
                 ) -> tuple[list[Mapping[str, Any]], str | None]:
        """One listing page and the URL of the next one, or None at the end.

        The next URL is the same-collection ``@odata.nextLink`` check of
        ``get_pages`` (the request's own path, only the server's continuation
        query), so a caller may persist it and resume a traversal later; a
        nextLink that leaves the collection (or changes a BC query) raises
        ``nextlink_refused`` before anything is returned or persisted."""
        page = self.get_json(url, headers=headers)
        if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
            raise SourceUnavailable(f"{self.source} listing lacks value list")
        items = list(page["value"])
        if not all(isinstance(item, Mapping) for item in items):
            raise SourceUnavailable(f"{self.source} listing item invalid")
        continuation = page.get("@odata.nextLink")
        if continuation is None:
            return items, None
        next_url = self.continuation_url(url, continuation)
        if next_url is None or next_url == url:
            raise nextlink_refused(f"{self.source} nextLink escaped the collection")
        return items, next_url

    def get_pages(self, url: str, *, max_pages: int, max_items: int,
                  headers: Mapping[str, str] | None = None) -> tuple[list[Mapping[str, Any]],
                                                                     dict[str, Any]]:
        """Follow same-collection ``@odata.nextLink`` within page and item caps.

        A ``SourceUnavailable`` raised mid-read carries ``pages_read`` and
        ``items_read`` of the pages read before it, so a caller can report
        what was read before the refusal."""
        if not isinstance(max_pages, int) or not 1 <= max_pages <= 50:
            raise ValueError("max_pages must be 1..50")
        if not isinstance(max_items, int) or not 1 <= max_items <= 5000:
            raise ValueError("max_items must be 1..5000")
        items: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        next_url: str | None = url
        reason = None
        try:
            while next_url is not None:
                if next_url in seen:
                    raise SourceUnavailable(f"{self.source} pagination loop")
                seen.add(next_url)
                page = self.get_json(next_url, headers=headers)
                pages += 1
                if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                    raise SourceUnavailable(f"{self.source} listing lacks value list")
                for item in page["value"]:
                    if not isinstance(item, Mapping):
                        raise SourceUnavailable(f"{self.source} listing item invalid")
                    if len(items) >= max_items:
                        reason = "item_cap"
                        break
                    items.append(item)
                continuation = page.get("@odata.nextLink")
                if reason == "item_cap":
                    break
                if continuation is None:
                    next_url = None
                    break
                if pages >= max_pages:
                    # The cap decides first: a continuation this read will never
                    # follow is not inspected (honest truncation, not an error).
                    reason = "page_cap"
                    break
                next_url = self.continuation_url(url, continuation)
                if next_url is None:
                    raise nextlink_refused(f"{self.source} nextLink escaped the collection")
        except SourceUnavailable as exc:
            # A refusal or failure after some pages were read keeps the counts of
            # what was read before it (the items are discarded, never returned).
            exc.pages_read, exc.items_read = pages, len(items)
            raise
        truncated = reason is not None
        return items, {"complete": not truncated, "truncated": truncated,
                       "truncation_reason": reason, "pages_read": pages,
                       "page_cap": max_pages, "item_cap": max_items,
                       "items_read": len(items)}


# OData addresses one entity by key either as a path segment (``/x/<id>``) or
# in key syntax (``/x('<id>')``, BC ``/companies(<guid>)``); Graph answers a
# listing requested as ``mailFolders/<id>/messages`` with a nextLink on
# ``mailFolders('<id>')/messages`` (live repro, directive 4f484b130e05).
_KEY_SEGMENT = re.compile(r"^([A-Za-z][A-Za-z0-9._-]{0,63})\((?:'([^'()]+)'|([^'()]+))\)$")
_GUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                           r"[0-9a-fA-F]{12}$")


def _template_literals() -> frozenset[str]:
    """Lower-cased fixed path words of the closed grammar (navigation names)."""
    words = set(grammar.BC_ENTITY_SETS) | set(grammar.BC_DOCUMENT_LINES.values())
    for _host, template in grammar.TEMPLATES.values():
        for segment in template.split("/"):
            literal = segment.split("(", 1)[0]
            if literal and "{" not in literal and ":" not in literal:
                words.add(literal)
    return frozenset(word.lower() for word in words)


_PATH_LITERALS = _template_literals()


def _canonical_segments(path: str) -> list[str] | None:
    """``/a/b('k')/c`` and ``/a/b/k/c`` -> ``[a, b, k, c]``; None if unsafe.

    Each segment is percent-decoded exactly once, then must carry no ``/``,
    ``\\``, ``%``, ``..``, control character, or stray quote/parenthesis
    outside one well-formed key (so ``%2F`` or ``%252F`` inside a key, ``../``
    and composite keys ``x(a='b')`` all refuse)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    result: list[str] = []
    for raw in path.split("/")[1:]:
        segment = unquote(raw)
        if (not segment or "/" in segment or "\\" in segment or "%" in segment
                or ".." in segment or segment == "."
                or any(ord(c) < 32 or ord(c) == 127 for c in segment)):
            return None
        if any(c in segment for c in "()'"):
            keyed = _KEY_SEGMENT.fullmatch(segment)
            if keyed is None:
                return None
            result.extend((keyed[1], keyed[2] if keyed[2] is not None else keyed[3]))
        else:
            result.append(segment)
    return result


def _same_segment(requested: str, linked: str) -> bool:
    """Navigation words and GUIDs compare case-insensitively (Graph/BC treat
    them so); every other id (mail, folder, drive, item ids are opaque and
    case-sensitive) must be identical."""
    if requested == linked:
        return True
    if requested.lower() != linked.lower():
        return False
    return (requested.lower() in _PATH_LITERALS
            or _GUID_SEGMENT.fullmatch(requested) is not None)


# Microsoft Graph server-driven paging: a nextLink repeats the request's own
# OData options and adds a continuation (``$skiptoken`` on drive children,
# permissions and delta pages; ``$skip`` on message listings; ``$deltatoken``
# on a delta round; ``token`` on a drive's change feed, ``root/delta``; ``$top``
# repeated or re-stated). Only these may be new; any other parameter in the
# server's query fails closed. BC continuations: ``_bc_continuation_ok`` (and
# ``live_source_grammar.bc_request_query_ok`` in ``check_request``).
GRAPH_CONTINUATION_PARAMS = frozenset({"$skiptoken", "$skip", "$top", "$deltatoken", "token"})
_GRAPH_CONTINUATION_VALUE = {
    "$skip": re.compile(r"[0-9]{1,7}"),
    "$top": re.compile(r"[1-9][0-9]{0,3}"),
    # Opaque server tokens: printable ASCII, bounded (sent back verbatim).
    "$skiptoken": re.compile(r"[\x20-\x7e]{1,2048}"),
    "$deltatoken": re.compile(r"[\x20-\x7e]{1,2048}"),
    "token": re.compile(r"[\x20-\x7e]{1,2048}"),
}


_QUERY_NAME = re.compile(r"\$?[A-Za-z][A-Za-z0-9_-]{0,63}")


def _raw_pairs(query: str) -> list[tuple[str, str]] | None:
    """``name=value`` pairs of a raw query, values left encoded; names are
    percent-decoded once (``%24skip`` -> ``$skip``) and must be plain option
    names. None on an empty pair, a pair without ``=`` or an odd name."""
    if not query:
        return []
    pairs = []
    for part in query.split("&"):
        name, sep, value = part.partition("=")
        name = unquote(name)
        if not sep or _QUERY_NAME.fullmatch(name) is None:
            return None
        pairs.append((name, value))
    return pairs


def _graph_continuation_ok(request_query: str, link_query: str) -> bool:
    """Whether a Graph nextLink query is the request's own options plus allowed
    continuation parameters.

    Each name appears once. A continuation parameter must match its bounded
    value shape (checked on the value percent-decoded once, ``+`` kept
    literal), and a ``$top`` may not exceed the request's own ``$top``. A
    repeated request option must be byte-identical to the request's raw value
    (what the server already read), or carry no ``+`` and percent-decode to
    the request's value: a ``+`` the server may read literally is never
    compared as a space; or keep every raw ``+`` at the same place with the
    rest percent-decoding to the same bytes (``_same_plus_aligned``, Graph's
    echo of a filtered request). Options the request did not send and that are not
    continuation parameters (``$expand``, ``$search``, a widened ``$filter``,
    custom names ...) refuse.

    Known limitation: only additions are bounded. A misbehaving server can
    drop request options (``$filter``, ``$orderby``, ``$select``) from the
    link; the read then stays inside the same collection and every item still
    passes the post-read provenance and audience checks, but it may be wider
    or ordered differently than requested. Requiring every option would break
    skiptoken-only links, which encode the options in the token."""
    wanted, given = _raw_pairs(request_query), _raw_pairs(link_query)
    if wanted is None or given is None:
        return False
    requested = dict(wanted)
    names = [name for name, _value in given]
    if len(set(names)) != len(names) or len(requested) != len(wanted):
        return False
    try:
        decoded_request = dict(parse_qsl(request_query, keep_blank_values=True,
                                         strict_parsing=True)) if request_query else {}
    except ValueError:
        return False
    for name, raw in given:
        shape = _GRAPH_CONTINUATION_VALUE.get(name)
        if shape is not None:
            value = unquote(raw)
            if shape.fullmatch(value) is None:
                return False
            if name == "$top" and "$top" in decoded_request:
                limit = decoded_request["$top"]
                if not limit.isdigit() or int(value) > int(limit):
                    return False
            continue
        if name not in requested:
            return False
        if raw == requested[name] or _same_plus_aligned(requested[name], raw):
            continue
        # The request was built by ``urlencode`` ('+' = space), so it is
        # form-decoded; the link value is decoded without '+' handling.
        if "+" in raw or unquote(raw) != decoded_request.get(name):
            return False
    return True


_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _same_plus_aligned(request_raw: str, link_raw: str) -> bool:
    """Whether two raw option values mean the same under every reading of '+'.

    Rule: both values split on the raw ``+`` into the same number of segments,
    and each pair of segments percent-decodes once (to bytes, ``+`` never
    touched) to identical bytes; a ``%`` not followed by two hex digits in
    either value refuses. So the raw ``+`` positions coincide and only the
    escaping of the other bytes may differ: Graph echoes a ``$filter`` sent as
    ``receivedDateTime+ge+2026-09-01T00%3A00%3A00Z`` as
    ``receivedDateTime+ge+2026-09-01T00:00:00Z`` (live directive 4458d0e6d280).

    Why this cannot admit a different filter: a server decodes a value either
    as a form (``+`` -> space, then percent-decode) or percent-only (``+``
    literal). Both decoders work segment by segment between raw ``+``
    characters and map each raw ``+`` to the same character, so equal segment
    bytes and equal ``+`` positions give equal decoded values under either
    decoder. A ``+`` on one side against ``%20`` or ``%2B`` on the other changes
    the segment count and refuses; byte comparison keeps two different invalid
    UTF-8 sequences from both reading as U+FFFD."""
    if _PERCENT_ESCAPE.search(request_raw) or _PERCENT_ESCAPE.search(link_raw):
        return False
    wanted, given = request_raw.split("+"), link_raw.split("+")
    try:
        return len(wanted) == len(given) and all(
            unquote_to_bytes(text_bytes(a)) == unquote_to_bytes(text_bytes(b))
            for a, b in zip(wanted, given))
    except ValueError:  # text_not_unicode: never matches
        return False


# BC server-driven paging (documented, UNVERIFIED live: ``live_source_grammar.
# _BC_CONTINUATION``): a nextLink repeats the request's own options and adds ``aid``
# and an opaque ``$skiptoken``. Those and a ``$top`` no higher than the request's own
# are the paging parameters; every other option must be the request's own.
BC_PAGING_PARAMS = frozenset({"$skiptoken", "aid", "$top"})


def _bc_continuation_ok(request_path: str, request_query: str, link_query: str) -> bool:
    """Whether a BC nextLink query continues the same read (p0-f-bc).

    The link query must pass the guard's own BC check for the request's path
    (``grammar.bc_request_query_ok``, so an accepted link is never refused later by
    ``check_request``) and carry exactly the request's non-paging options
    (``$filter``, ``$select``, ``$orderby``): none dropped, added or changed. A value
    is the request's when byte-identical, ``+``-aligned (``_same_plus_aligned``) or
    free of ``+`` and percent-decoding to the request's value (the rule of
    ``_graph_continuation_ok``). ``$top`` may restate or lower the request's own
    (OData servers count it down) and is refused when the request had none."""
    if not grammar.bc_request_query_ok(request_path, link_query):
        return False
    wanted, given = _raw_pairs(request_query), _raw_pairs(link_query)
    if wanted is None or given is None:
        return False
    requested, linked = dict(wanted), dict(given)
    if len(requested) != len(wanted) or len(linked) != len(given):
        return False
    try:
        decoded = dict(parse_qsl(request_query, keep_blank_values=True,
                                 strict_parsing=True)) if request_query else {}
    except ValueError:
        return False
    options = set(requested) - BC_PAGING_PARAMS
    if set(linked) - BC_PAGING_PARAMS != options:
        return False
    for name in options:
        raw = linked[name]
        if not (raw == requested[name] or _same_plus_aligned(requested[name], raw)
                or ("+" not in raw and unquote(raw) == decoded.get(name))):
            return False
    if "$top" in linked:
        limit, value = decoded.get("$top"), unquote(linked["$top"])
        if (limit is None or not (limit.isdigit() and value.isdigit())
                or int(value) > int(limit)):
            return False
    return True


def _same_collection_next(url: str, continuation: Any, *,
                          environ: Mapping[str, str] | None = None) -> str | None:
    """The URL to GET for a nextLink, or None when it leaves the collection.

    Scheme, host and the collection path are strict: the nextLink must be
    plain https on the request's host, and its path, after key syntax is
    canonicalised on both sides, must name the same segments; its query must
    continue the request's (``_graph_continuation_ok`` / ``_bc_continuation_ok``).
    The URL that is sent keeps the *request's* own path (built by the closed
    grammar) and takes only the continuation query from the server, so the
    server never chooses the path that is read. That URL must pass
    ``check_request`` (``environ`` as the transport's) and its query may carry
    no malformed ``%`` escape, so an accepted link is always one the guard
    sends: a raw space or control character refuses here, never mid-traversal."""
    if (not isinstance(continuation, str) or len(continuation) > 8192
            or any(ord(char) <= 32 or ord(char) == 127 for char in continuation)):
        # As given: ``urlsplit`` silently drops tabs and newlines, which would turn a
        # link ``check_request`` refuses into another, sendable one.
        return None
    try:
        request, link = urlsplit(url), urlsplit(continuation)
        link_port = link.port
    except ValueError:
        return None
    if (link.scheme != "https" or request.scheme != "https" or link_port is not None
            or link.username or link.password or link.fragment
            or not link.hostname or link.netloc.lower() != link.hostname
            or link.hostname != (request.hostname or "")):
        return None
    wanted, given = _canonical_segments(request.path), _canonical_segments(link.path)
    if (wanted is None or given is None or len(wanted) != len(given)
            or not all(_same_segment(a, b) for a, b in zip(wanted, given))):
        return None
    if link.hostname == GRAPH_HOST and not _graph_continuation_ok(request.query, link.query):
        return None
    if link.hostname == BC_HOST and not _bc_continuation_ok(request.path, request.query,
                                                            link.query):
        return None
    result = f"https://{request.netloc}{request.path}" + (f"?{link.query}" if link.query else "")
    if _PERCENT_ESCAPE.search(link.query):
        return None
    try:
        check_request("GET", result, environ=environ)
    except ReadOnlyGuardError:
        return None
    return result


# ------------------------------------------------------------------ audience

# Spaces are allowed (SharePoint folder names have them); control characters
# and inner ``*`` are not.
_RESOURCE = re.compile(r"^(mail|sharepoint|bc|teams|mc|repo):[^\x00-\x1f\x7f*]{1,1024}$")
_PATTERN = re.compile(r"^(mail|sharepoint|bc|teams|mc|repo):[^\x00-\x1f\x7f*]{0,1024}\*?$")
# A code repository a case original was committed to (``repo:<owner>/<name>``, GitHub's
# name grammar; no reader fetches from it, ``operating.captured_originals`` serves the
# bytes the local object store holds). Granted by exact pattern only: never ``*``, no
# prefix, so each grant names one repository; matched case-insensitively as GitHub names.
_REPOSITORY_RESOURCE = re.compile(r"repo:([A-Za-z0-9_.\-]{1,100})/([A-Za-z0-9_.\-]{1,100})")


def _repository_resource_ok(value: str) -> bool:
    found = _REPOSITORY_RESOURCE.fullmatch(value)
    return found is not None and not any(set(part) == {"."} for part in found.groups())


def _repository_key_bad(resource: str) -> bool:
    """A ``repo:`` resource malformed as given, decided before any lowercasing (a
    non-ASCII character that lowercases to ASCII, such as the Kelvin sign, never
    reaches a grant)."""
    return resource.startswith("repo:") and not _repository_resource_ok(resource)
# A Teams conversation is visible only to its members and a SharePoint list item only to
# those its own permissions name: every ``teams:`` and ``sharepoint:list:`` resource needs
# the item's own source ACL (the membership or permissions captured with it), whatever the
# map's ``source_acl_required`` says, so a grant can never widen a channel, chat or list.
_ALWAYS_SOURCE_ACL = ("teams:", "sharepoint:list:")


def _strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("live_source_audience_duplicate_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _match(table: Mapping[str, Any], resource: str) -> Any:
    if resource in table:
        return table[resource]
    best = None
    for pattern, value in table.items():
        if pattern.endswith("*") and resource.startswith(pattern[:-1]):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, value)
    return None if best is None else best[1]


class LiveSourceAudience:
    """Resource audience for live sources: default deny, most specific pattern wins.

    Mirrors ``source_mirror.AudienceMap`` (tenant, clearance >= classification,
    principal intersection, no ``*`` principals). ``identities`` maps a
    Workbench principal to the source identities it stands for (mailbox UPNs,
    Entra object ids, ``group:<id>``); ``source_acl_required`` lists resource
    patterns whose items must also grant one of those identities in their own
    source ACL. The map is configuration, not authority.
    """

    def __init__(self, audiences: Mapping[str, Iterable[str]], *, tenant: str,
                 classifications: Mapping[str, str] | None = None,
                 default_classification: str = "confidential",
                 identities: Mapping[str, Iterable[str]] | None = None,
                 source_acl_required: Iterable[str] = ()) -> None:
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("live_source_audience_tenant_required")
        if default_classification not in LEVELS:
            raise ValueError("live_source_audience_classification_invalid")
        self.tenant = tenant.strip()
        self.default_classification = default_classification
        self._audiences: dict[str, frozenset[str]] = {}
        for pattern, principals in dict(audiences).items():
            self._check_pattern(pattern)
            values = self._principals(principals)
            self._audiences[pattern.lower()] = frozenset(values)
        self._classifications = {}
        for pattern, level in dict(classifications or {}).items():
            self._check_pattern(pattern)
            if level not in LEVELS:
                raise ValueError("live_source_audience_classification_invalid")
            self._classifications[pattern.lower()] = level
        self._identities: dict[str, frozenset[str]] = {}
        for principal, values in dict(identities or {}).items():
            self._principals([principal])
            if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
                raise ValueError("live_source_audience_identities_invalid")
            ids = list(values)
            if any(not isinstance(v, str) or not v.strip() or "*" in v or len(v) > 320
                   for v in ids):
                raise ValueError("live_source_audience_identities_invalid")
            self._identities[principal] = frozenset(v.strip().lower() for v in ids)
        required = list(source_acl_required)
        for pattern in required:
            self._check_pattern(pattern)
        self._acl_required = frozenset(p.lower() for p in required)

    @staticmethod
    def _check_pattern(pattern: Any) -> None:
        if not isinstance(pattern, str) or _PATTERN.fullmatch(pattern) is None:
            raise ValueError("live_source_audience_pattern_invalid")
        if pattern.lower().startswith("repo:") and not _repository_resource_ok(pattern):
            raise ValueError("live_source_audience_pattern_invalid")

    @staticmethod
    def _principals(principals: Any) -> list[str]:
        if isinstance(principals, (str, bytes)) or not isinstance(principals, Iterable):
            raise ValueError("live_source_audience_principals_invalid")
        values = list(principals)
        if any(not isinstance(item, str) or not item.strip() or item != item.strip()
               or "*" in item or len(item) > 200 for item in values):
            raise ValueError("live_source_audience_principal_invalid")
        return values

    @classmethod
    def from_json(cls, document: Any) -> "LiveSourceAudience":
        allowed = {"schema", "tenant", "audiences", "classifications",
                   "default_classification", "identities", "source_acl_required", "note"}
        if (not isinstance(document, dict) or document.get("schema") != LIVE_AUDIENCE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("audiences"), dict)):
            raise ValueError("live_source_audience_document_invalid")
        for principals in document["audiences"].values():
            if not isinstance(principals, list):
                raise ValueError("live_source_audience_principals_invalid")
        return cls(document["audiences"], tenant=document.get("tenant"),
                   classifications=document.get("classifications", {}),
                   default_classification=document.get("default_classification",
                                                       "confidential"),
                   identities=document.get("identities", {}),
                   source_acl_required=document.get("source_acl_required", []))

    @classmethod
    def from_file(cls, path: str | Path) -> "LiveSourceAudience":
        return cls.from_json(_strict_json(Path(path).read_bytes()))

    @staticmethod
    def _lookup(table: Mapping[str, Any], key: str) -> Any:
        """``_match``, except that a ``repo:`` resource matches its exact pattern only."""
        if key.startswith("repo:"):
            return table.get(key) if _repository_resource_ok(key) else None
        return _match(table, key)

    def classification_for(self, resource: str) -> str:
        if _repository_key_bad(resource):
            return "restricted"
        return self._lookup(self._classifications, resource.lower()) \
            or self.default_classification

    def allows(self, actor: Actor, resource: str) -> bool:
        if (not isinstance(actor, Actor) or not isinstance(resource, str)
                or _RESOURCE.fullmatch(resource) is None or actor.tenant != self.tenant
                or _repository_key_bad(resource)):
            return False
        key = resource.lower()
        if LEVELS[self.classification_for(key)] > LEVELS[actor.clearance]:
            return False
        return bool((self._lookup(self._audiences, key) or frozenset()) & actor.principals)

    def principals_for(self, resource: str) -> frozenset[str]:
        """The principals the most specific pattern grants ``resource`` (before the
        tenant and clearance checks of ``allows``); empty when none or malformed."""
        if not isinstance(resource, str) or _RESOURCE.fullmatch(resource) is None \
                or _repository_key_bad(resource):
            return frozenset()
        return self._lookup(self._audiences, resource.lower()) or frozenset()

    def granted_sources(self) -> frozenset[str]:
        """The source families (``mail``, ``sharepoint``, ``bc``, ``teams``, ``mc``,
        ``repo``) some pattern grants to a principal: under this map nobody reads any other
        family."""
        return frozenset(pattern.split(":", 1)[0]
                         for pattern, principals in self._audiences.items() if principals)

    def may_read_any(self, actor: Actor, prefix: str) -> bool:
        """Whether any grant under ``prefix`` could apply to ``actor``.

        Used before resolving a canonical identity, so an actor with no grant
        in a source family causes no source I/O at all.
        """
        if not isinstance(actor, Actor) or actor.tenant != self.tenant:
            return False
        prefix = prefix.lower()
        for pattern, principals in self._audiences.items():
            if not principals & actor.principals:
                continue
            if pattern.startswith(prefix) or (pattern.endswith("*") and
                                              prefix.startswith(pattern[:-1])):
                return True
        return False

    def require(self, actor: Actor, resource: str) -> None:
        if not self.allows(actor, resource):
            raise NotVisible()

    def allows_all(self, actor: Actor, resources: Iterable[str]) -> bool:
        """Identity-set decision, most restrictive wins.

        ``resources`` are every key one source object answers to (a
        mailbox's object id, UPN, mail and SMTP proxy addresses; a folder's
        own path and its container ``<path>/``). Keys no pattern matches are
        ignored; at least one key must match; every matched entry (the most
        specific per key) must grant the actor, and the strictest
        classification of all keys must be within the actor's clearance.
        """
        given = [r for r in resources if isinstance(r, str)]
        keys = [r.lower() for r in given]
        if (not isinstance(actor, Actor) or actor.tenant != self.tenant or not keys
                or any(_RESOURCE.fullmatch(k) is None for k in keys)
                or any(_repository_key_bad(r) for r in given)):
            return False
        matched = [self._lookup(self._audiences, key) for key in keys]
        if all(m is None for m in matched):
            return False
        if any(m is not None and not (m & actor.principals) for m in matched):
            return False
        strictest = max(LEVELS[self.classification_for(key)] for key in keys)
        return strictest <= LEVELS[actor.clearance]

    def require_all(self, actor: Actor, resources: Iterable[str]) -> None:
        if not self.allows_all(actor, resources):
            raise NotVisible()

    def covers_whole(self, actor: Actor, container: str) -> bool:
        """Whether ``actor`` may read *everything* under ``container`` (a key such as
        ``sharepoint:<drive>:/``) with no narrower rule anywhere below it: the
        container is allowed, every audience pattern inside it grants the actor,
        every classification inside it is within clearance and no item under it
        needs its own source ACL. A collection read that cannot check items one by
        one (a library-wide list read) is permitted only then."""
        if not self.allows(actor, container) or self.source_acl_required(container):
            return False
        key = container.lower()
        if any(p.startswith(key) or key.startswith(p.rstrip("*")) for p in self._acl_required):
            return False
        if any(p.startswith(key) and not principals & actor.principals
               for p, principals in self._audiences.items()):
            return False
        return all(LEVELS[level] <= LEVELS[actor.clearance]
                   for p, level in self._classifications.items() if p.startswith(key))

    def source_acl_required_any(self, resources: Iterable[str]) -> bool:
        return any(self.source_acl_required(r) for r in resources)

    def visible_all(self, actor: Actor, resources: Sequence[str],
                    source_acl: Mapping[str, Any]) -> bool:
        """``allows_all`` plus, where any key requires it, the item's source ACL."""
        if not self.allows_all(actor, resources):
            return False
        if not self.source_acl_required_any(resources):
            return True
        if source_acl.get("status") not in ("read", "derived"):
            return False
        granted = {str(v).lower() for v in source_acl.get("identities", [])}
        return bool(granted & self.identities_for(actor))

    def source_acl_required(self, resource: str) -> bool:
        key = resource.lower()
        if key.startswith(_ALWAYS_SOURCE_ACL):
            return True
        return any(key == p or (p.endswith("*") and key.startswith(p[:-1]))
                   for p in self._acl_required)

    def identities_for(self, actor: Actor) -> frozenset[str]:
        found: set[str] = set()
        for principal in actor.principals:
            found |= self._identities.get(principal, frozenset())
        return frozenset(found)

    def item_visible(self, actor: Actor, resource: str, source_acl: Mapping[str, Any]) -> bool:
        """Resource audience plus, where required, the item's own source ACL."""
        if not self.allows(actor, resource):
            return False
        if not self.source_acl_required(resource):
            return True
        if source_acl.get("status") not in ("read", "derived"):
            return False
        granted = {str(v).lower() for v in source_acl.get("identities", [])}
        return bool(granted & self.identities_for(actor))


# ------------------------------------------------------------------ citations

def q(value: str) -> str:
    return quote(value, safe="")


def completeness_with_withheld(completeness: Mapping[str, Any], withheld: int) -> dict[str, Any]:
    result = dict(completeness)
    result["withheld_count"] = withheld
    result["items_returned"] = result.get("items_read", 0) - withheld
    return result


def parse_handle(handle: Any, scheme: str, parts: int) -> tuple[list[str], str, dict[str, str]]:
    """``<scheme>://p1/p2/.../pn@version[#k=v&...]`` → (parts, version, fragment)."""
    if not isinstance(handle, str) or len(handle) > 4096 or not handle.startswith(scheme + "://"):
        raise ValueError("citation_handle_invalid")
    body, _, fragment = handle[len(scheme) + 3:].partition("#")
    path, sep, version = body.rpartition("@")
    segments = path.split("/")
    if not sep or len(segments) != parts or any(not s for s in segments) or not version:
        raise ValueError("citation_handle_invalid")
    extra = {}
    if fragment:
        for key, values in parse_qs(fragment, strict_parsing=True).items():
            extra[key] = values[0]
    return [unquote(s) for s in segments], unquote(version), extra


__all__ = ["ALLOWED_HOSTS", "BACKOFF_CAP_SECONDS", "BC_SCOPE", "BoundedBackoff",
           "ClientCredentials", "DENIED_PATH_TOKENS",
           "DownloadIncomplete", "ERROR_CODE_TOKENS", "MESSAGE_CLASS_TOKENS",
           "MAX_RETRIES", "failure_diagnostics", "is_error_code_token", "upstream_error_code",
           "upstream_message_class",
           "RETRY_STATUSES", "is_transport_failure", "transport_failure",
           "is_nextlink_refusal", "nextlink_refused",
           "GRAPH_SCOPE", "GuardedTransport", "LIVE_AUDIENCE_SCHEMA", "LiveSourceAudience",
           "MAX_ATTACHMENT_RESPONSE_BYTES",
           "NotVisible", "ReadOnlyGuardError", "RequestBudget", "RequestBudgetExhausted",
           "RequestDeadline", "RequestDeadlinePassed",
           "Secret", "SourceApi", "SourceNotFound",
           "SourceUnavailable", "TokenProvider", "canonical_sha256", "check_request",
           "jsonable", "parse_handle", "sharepoint_host_from_env", "tenant_download_hosts",
           "text_bytes", "urllib_sender"]
