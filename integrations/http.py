"""Small HTTPS-only GET transport shared by read-only integrations."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceUnavailable(RuntimeError):
    """Source cannot be read or did not return its documented contract."""


class SourceHttpError(SourceUnavailable):
    """An HTTP error status from a read-only source.

    Carries only what a caller needs to pace or restart a read: the status, the
    ``Retry-After`` delay in seconds (``None`` when absent or unparseable) and a
    short machine error code from a JSON ``{"error": {"code": ...}}`` body.
    The message stays the generic one; the body text is never kept.
    """

    def __init__(self, message: str, *, status: int, retry_after_seconds: float | None = None,
                 error_code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.error_code = error_code


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def retry_after_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    """RFC 9110 ``Retry-After``: delay-seconds or an HTTP-date, as seconds >= 0."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - (now or datetime.now(timezone.utc))).total_seconds())


def _http_error(exc: HTTPError) -> SourceHttpError:
    code = None
    try:
        raw = exc.read(4097)
        if len(raw) <= 4096:
            body = json.loads(raw)
            candidate = body.get("error", {}).get("code") if isinstance(body, dict) else None
            if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate):
                code = candidate
    except Exception:  # the body is optional detail; the status is the contract
        code = None
    headers = getattr(exc, "headers", None)
    delay = retry_after_seconds(headers.get("Retry-After") if headers is not None else None)
    return SourceHttpError("read-only source GET failed: HTTPError", status=int(exc.code),
                           retry_after_seconds=delay, error_code=code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceUnavailable("read-only source redirected; refusing credential forwarding")


def check_https_origin(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.fragment:
        raise ValueError("source URL must be a plain HTTPS origin")


def check_https_base(url: str) -> None:
    check_https_origin(url)
    parts = urlsplit(url)
    if parts.path not in ("", "/") or parts.query:
        raise ValueError("source base URL must contain only an HTTPS origin")


def get_json(url: str, headers: Mapping[str, str], *, max_bytes: int = 4_000_000,
             timeout_seconds: float = 15) -> Any:
    """Perform one GET, never follow a redirect, and omit credentials from errors."""
    check_https_origin(url)
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 28_000_000:
        raise ValueError("source response bound must be 1..28 MB")
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
        raise ValueError("source timeout must be 0..60 seconds")
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise SourceUnavailable("source response exceeds configured bound")
            return json.loads(raw)
    except SourceUnavailable:
        raise
    except HTTPError as exc:
        raise _http_error(exc) from None
    except Exception as exc:
        raise SourceUnavailable(f"read-only source GET failed: {type(exc).__name__}") from None
