"""Shared plumbing of the order-intake and meeting connectors (Zendesk, Shopify, Granola):
disabled until the credential settings exist, one GET-only guarded transport, bounded retry
on 429/5xx (``Retry-After`` honoured within the guard's cap), a hard wall-clock deadline and
request budget per sweep, and a cursor file that records what was last read so a source is
read incrementally and its freshness is a fact, not a guess.

Settings are read by name only; values are never printed, logged, stored or committed. A
vendor payload that is not the documented shape fails closed (``SourceUnavailable``), never
guessed. No network I/O happens here except through ``GuardedTransport``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .http import SourceUnavailable
from .live_source_guard import (BoundedBackoff, GuardedTransport, RequestBudget, RequestDeadline,
                                ReadOnlyGuardError, failure_diagnostics)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_DEADLINE_SECONDS = 240.0
DEFAULT_MAX_REQUESTS = 400
FRESH_FOR = timedelta(minutes=30)       # a capture older than this reads as stale


class CredentialsAbsent(SourceUnavailable):
    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__("credentials_absent: " + ", ".join(self.missing))


class SettingInvalid(SourceUnavailable):
    def __init__(self, name: str) -> None:
        self.setting = name
        super().__init__(f"setting_invalid: {name}")


def missing_settings(required: Sequence[str], environ: Mapping[str, str] | None = None
                     ) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in required if not (env.get(name) or "").strip()]


def enabled(required: Sequence[str], environ: Mapping[str, str] | None = None) -> bool:
    """A connector is on only when every required setting is present."""
    return not missing_settings(required, environ)


def text(value: Any, limit: int = 200) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


class VendorClient:
    """GET one JSON document through the guarded transport with bounded retries."""

    def __init__(self, transport: GuardedTransport, auth_headers: Mapping[str, str], *,
                 backoff: BoundedBackoff | None = None) -> None:
        self.transport = transport
        self._auth = dict(auth_headers)
        self.backoff = backoff or BoundedBackoff()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def get_json(self, url: str) -> tuple[Any, Mapping[str, str]]:
        headers = {"Accept": "application/json", **self._auth}
        self.backoff.request()
        attempt = 0
        while True:
            self.backoff.hold()
            status, response_headers, raw = self.transport.request("GET", url, headers)
            if status in RETRY_STATUSES and self.backoff.may_retry(attempt, response_headers):
                self.backoff.pause(attempt, response_headers)
                attempt += 1
                continue
            break
        if status != 200:
            error = SourceUnavailable(f"GET returned HTTP {status}")
            error.http_status = status  # type: ignore[attr-defined]
            raise error
        try:
            return json.loads(raw), response_headers
        except ValueError:
            raise SourceUnavailable("response is not JSON") from None


class SweepState:
    """Per-source cursor and freshness in one JSON file, written atomically (temp file in the
    same directory, then rename), so a crash leaves the last complete state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def get(self, source: str) -> dict[str, Any]:
        entry = self.read().get(source)
        return dict(entry) if isinstance(entry, dict) else {}

    def put(self, source: str, entry: Mapping[str, Any]) -> None:
        doc = self.read()
        doc[source] = dict(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freshness(entry: Mapping[str, Any], now: datetime, *, fresh_for: timedelta = FRESH_FOR
              ) -> dict[str, Any]:
    """``never_captured`` / ``fresh`` / ``stale`` from the last complete sweep's time."""
    raw = entry.get("last_complete_at")
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return {"state": "never_captured", "newest_captured": None}
    if last.tzinfo is None:
        return {"state": "never_captured", "newest_captured": None}
    return {"state": "fresh" if now - last <= fresh_for else "stale",
            "newest_captured": _iso(last), "age_seconds": int((now - last).total_seconds())}


class CaptureFailed(Exception):
    """The capture store refused or failed a sweep's records (``code``: its error code or
    the exception type); the cursor is not moved."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _persist(persist: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]],
             records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    try:
        return dict(persist(records))
    except Exception as exc:  # noqa: BLE001 - any store failure keeps the cursor
        raise CaptureFailed(str(getattr(exc, "code", "") or type(exc).__name__)[:80]) from None


def run_sweep(source: str, state: SweepState, step: Callable[[dict[str, Any]], dict[str, Any]],
              transport: GuardedTransport, *, now: datetime | None = None,
              deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
              max_requests: int = DEFAULT_MAX_REQUESTS,
              persist: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]] | None = None
              ) -> dict[str, Any]:
    """One bounded sweep. ``step(entry)`` reads from the saved cursor and returns
    ``{records, cursor, complete}``. ``persist(records)`` (the capture store's save,
    ``integrations.capture_vendor``) runs before the cursor is saved: when it fails the
    sweep reports ``capture_failed`` and keeps the previous cursor, so the next sweep reads
    the same records again and nothing is skipped."""
    now = now or datetime.now(timezone.utc)
    entry = state.get(source)
    budget = RequestBudget(transport, max_requests)
    out: dict[str, Any] = {"source": source, "records": [], "complete": False}
    try:
        with RequestDeadline(deadline_seconds).active(), budget.active():
            result = step(entry)
        out["records"] = result["records"]
        out["complete"] = bool(result.get("complete"))
        if result.get("refused"):
            out["refused"] = result["refused"]
        if persist is not None:
            out["capture"] = _persist(persist, out["records"])
        saved = {**entry, "cursor": result.get("cursor"), "last_run_at": _iso(now)}
        if out["complete"]:
            saved["last_complete_at"] = _iso(now)
        state.put(source, saved)
    except CaptureFailed as exc:
        out["error"] = "capture_failed"
        out["capture_error"] = exc.code
        out["complete"] = False
        state.put(source, {**entry, "last_error_at": _iso(now), "last_error": "capture_failed"})
    except (SourceUnavailable, ReadOnlyGuardError) as exc:
        out["error"] = type(exc).__name__
        out.update({k: v for k, v in failure_diagnostics(exc).items() if v is not None})
        state.put(source, {**entry, "last_error_at": _iso(now), "last_error": type(exc).__name__})
    out["requests_sent"] = transport.requests_sent
    out["freshness"] = freshness(state.get(source), now)
    return out
