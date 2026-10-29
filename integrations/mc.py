"""Mission Control clients: guarded REST reads and a guarded MCP tool client.

The deployed MC service remains the task, checkout and approval authority
(PLX_MC @ ``1fba0833``). The
key does not keep us read-only (every MCP principal but ``sp_mcp_portal``
carries MC's write bundle and every MC write is mirrored to production
SharePoint lists), so every request passes ``plx_hub_guard`` before I/O:

* ``MissionControlReadClient``: REST ``GET /api/cursor/*`` read routes only.
* ``MissionControlMcpClient``: JSON-RPC ``POST /api/cursor/mcp`` with the read
  tools of pack §3 rows 1-12 (``call_tool``), read-only by default. Writes go
  only through ``call_write_tool``/``checkout_task`` and need all of: the
  client was built with that tool in ``write_tools`` (explicit opt-in; only
  the pack's W-A rows can be opted in, never the NO rows), a
  ``plx_hub_guard.McWriteAuthorization`` (a recorded human approval naming
  that exact call and a configured ``write_approvers`` human who is not our
  MC operator, ``+tag`` aliases normalized; its origin is not
  cryptographically verified, MC's decided gate stays the authority) inside
  its validity window, and not used before: single use is a durable,
  fsynced, file-locked ``WriteAuthorizationLedger`` shared by every client
  and process using that file, checked and recorded before any I/O. A write
  client without a ledger cannot be built.
  ``mc_request_approval`` and ``mc_checkout_task`` are writes like any other.
  Writes are sent once (no retry): a lost answer is an unknown outcome.
* ``checkout_task`` always passes ``repo=petralabx/<name>`` and returns a
  typed ``CheckoutResult``: ``checked_out`` (only when MC's ``taskId`` and
  ``actor.repo`` match what we asked, PLX_MC ``src/lib/mcp/actions.ts:431-458``)
  or ``checkout_held`` (409, frontier P4 lease, PLX_MC
  ``docs/specs/frontier-implementation-spec.md:606``), never a new task.
* ``McAuditLog``: every MCP tool call (reads included, and refusals) and every
  REST read gets a local audit record: tool, argument names, outcome, error
  code, attempts, and HMAC-SHA-256 digests (per-log random salt, so short
  values cannot be guessed back) of the arguments and, for writes, of the
  authorization and approver. Never the key, an email or argument values. MC writes
  its own ``mcp.tool.invoked`` row as well (``create-http-server.ts:67-100``).
* Every answer a client-side guard refuses (a shape MC does not send, a usable
  checkout id, an unbound actor) raises ``McAnswerRefused`` (a ``SourceUnavailable``)
  whose ``code`` names that guard, so a caller records which guard refused.

Approvals are decided only in MC by a signed-in Entra admin/owner whose label
differs from the requester (``src/lib/compliance/approvals.ts:69-106``).
``approval_decision`` only *reads* a gate and re-checks the binding: our
operator requested it, its reason is exactly the action the caller expects,
and a human other than our operator and the requester decided it, with a
decision time. The operator email never grants anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit

from .http import SourceUnavailable, check_https_base
from .live_source_guard import ReadOnlyGuardError, Secret
from .plx_hub_guard import (CHECKOUT_REPO_OWNER, MC_APPROVABLE_WRITE_TOOLS, MC_HOSTS,
                            KNOWLEDGE_RETRY_STATUSES, MC_READ_TOOLS,
                            CheckoutHeld, HubTransport, McConfig, McWriteAuthorization,
                            UpstreamError, canonical_arguments, check_mc_request,
                            error_from_response, is_human_email, normalize_email)


Transport = Callable[[str, Mapping[str, str]], Any]
_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]+$")
_GATE_ID = re.compile(r"^apg_[A-Za-z0-9_-]{1,80}$")
_HUMAN_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_SERVICE_LABEL = re.compile(r"^(?:sp_|agent:|svc[_:-]|service[_:-])", re.IGNORECASE)
_USABLE_CHECKOUT = re.compile(r"dsp_[A-Za-z0-9]{8,}")
MC_AUDIT_SCHEMA = "plx-mc-call-audit/v1"
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_HELD_UNTIL = re.compile(r" until (\S+?)\.?$")


class McAuditLog:
    """Local, append-only audit of MC calls (bounded in memory, optional JSONL).

    Records carry no secret, operator email or argument value: only argument
    names and the SHA-256 of their canonical JSON. ``clock`` is injected.
    """

    MAX_RECORDS = 1000

    def __init__(self, path: Path | str | None = None, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: list[dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.Lock()
        # Digests are keyed with a random per-log salt (kept next to the JSONL,
        # mode 0600) so a TASK id + stage cannot be brute-forced back.
        self._salt = (_persistent_salt(self._path.with_name(self._path.name + ".salt"))
                      if self._path is not None else secrets.token_bytes(32))

    def _digest(self, text: str) -> str:
        return hmac.new(self._salt, text.encode(), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return f"McAuditLog(records={len(self._records)})"

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records]

    def record(self, *, door: str, tool: str, kind: str, arguments: Mapping[str, Any],
               outcome: str, error_code: str | None = None, attempts: int = 0,
               authorization: McWriteAuthorization | None = None) -> dict[str, Any]:
        try:
            digest = self._digest(canonical_arguments(arguments))
        except ReadOnlyGuardError:
            digest = None
        at = self._clock()
        with self._lock:
            self._seq += 1
            entry = {"schema": MC_AUDIT_SCHEMA, "seq": self._seq,
                     "at": at.astimezone(timezone.utc).isoformat(),
                     "door": door, "tool": tool[:80], "kind": kind,
                     "argument_names": sorted(str(k)[:40] for k in arguments),
                     "digest": "hmac-sha256/per-log-salt",
                     "arguments_hmac": digest, "outcome": outcome,
                     "error_code": error_code[:80] if isinstance(error_code, str) else None,
                     "attempts": attempts,
                     "authorization_hmac": (self._digest(authorization.identity)
                                            if authorization else None),
                     "approver_hmac": (self._digest(normalize_email(authorization.approved_by))
                                       if authorization else None)}
            self._records.append(entry)
            if len(self._records) > self.MAX_RECORDS:
                del self._records[0]
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return dict(entry)


def _persistent_salt(path: Path) -> bytes:
    """Read, or create exactly once (O_EXCL, 0600, fsynced), a 32-byte salt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raw = bytes.fromhex(path.read_text(encoding="ascii").strip())
        if len(raw) != 32:
            raise ValueError("salt file is corrupt") from None
        return raw
    salt = secrets.token_bytes(32)
    try:
        os.write(fd, salt.hex().encode("ascii") + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    return salt


class WriteAuthorizationLedger:
    """Durable single use for ``McWriteAuthorization`` (local JSONL file).

    Line 1 is a header with a random salt; every later line records one used
    authorization as ``HMAC(salt, identity)`` (tool, canonical arguments,
    normalized approver, approval time; not ``evidence_ref``/expiry, so a
    reworded copy of one approval is the same approval). ``consume`` takes an
    exclusive ``flock``, re-reads the whole file, refuses a known identity,
    appends and ``fsync``s, all before the caller sends anything. Every client
    and process pointed at the same file shares it. A missing header, a
    corrupt line or an unreadable file refuses (fail closed). Deleting the
    file resets it: keep it with the rest of the Workbench state.
    """

    SCHEMA = "plx-mc-write-ledger/v1"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            lines = self._read(fd)
            if not lines:
                header = {"schema": self.SCHEMA, "salt": secrets.token_hex(32)}
                os.write(fd, (json.dumps(header, sort_keys=True) + "\n").encode())
                os.fsync(fd)
                lines = [header]
            self._salt = self._header_salt(lines[0])
        finally:
            os.close(fd)  # releases the lock
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def __repr__(self) -> str:
        return f"WriteAuthorizationLedger(path={str(self._path)!r})"

    @staticmethod
    def _read(fd: int) -> list[dict[str, Any]]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        entries = []
        for line in b"".join(chunks).decode("utf-8").splitlines():
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("ledger line is not an object")
            entries.append(entry)
        return entries

    def _header_salt(self, header: dict[str, Any]) -> bytes:
        salt = header.get("salt")
        if header.get("schema") != self.SCHEMA or not isinstance(salt, str) or len(salt) != 64:
            raise ValueError("write ledger header invalid")
        return bytes.fromhex(salt)

    def key(self, authorization: McWriteAuthorization) -> str:
        return hmac.new(self._salt, authorization.identity.encode(), hashlib.sha256).hexdigest()

    def consume(self, authorization: McWriteAuthorization, *, at: datetime) -> None:
        """Record first use, durably, or raise ``ReadOnlyGuardError``."""
        key = self.key(authorization)
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_APPEND)
        except OSError:
            raise ReadOnlyGuardError("MC write ledger is unavailable") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                entries = self._read(fd)
                if not entries or self._header_salt(entries[0]) != self._salt:
                    raise ValueError("ledger header changed")
            except (ValueError, UnicodeDecodeError):
                raise ReadOnlyGuardError("MC write ledger is corrupt; refusing writes") from None
            if any(e.get("identity_hmac") == key for e in entries[1:]):
                raise ReadOnlyGuardError("MC write authorization already used")
            line = {"identity_hmac": key, "tool": authorization.tool,
                    "used_at": at.astimezone(timezone.utc).isoformat()}
            os.write(fd, (json.dumps(line, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


class McAnswerRefused(SourceUnavailable):
    """An MC answer a client-side guard refused; ``code`` names the guard (snake case, at
    most 40 characters), so a consumer records which guard refused, never MC's text."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _outcome(error: BaseException) -> tuple[str, str | None]:
    if isinstance(error, ReadOnlyGuardError):
        return "refused", "refused_by_guard"
    if isinstance(error, CheckoutHeld):
        return "held", "checkout_held"
    if isinstance(error, (UpstreamError, McAnswerRefused)):
        return "error", error.code
    return "error", type(error).__name__


@dataclass(frozen=True, repr=False)
class CheckoutResult:
    """Typed answer of ``checkout_task``.

    ``status`` is ``checked_out`` (``checkout_id`` holds the live ``dsp_*``
    bearer credential; ``pr_body_line`` is MC's exact stamp) or
    ``checkout_held`` (another principal holds the task+repo lease;
    ``held_until`` is MC's expiry text when present). Never a reason to
    create a new task (PLX_MC frontier spec :367).
    """

    status: str
    task_id: str
    repo: str
    checkout_id: Secret | None = None
    held_until: str | None = None
    message: str | None = None

    @property
    def pr_body_line(self) -> str | None:
        return "MC-Checkout: " + self.checkout_id.reveal() if self.checkout_id else None

    def __repr__(self) -> str:
        ref = ("dsp_…" + self.checkout_id.reveal()[-4:]) if self.checkout_id else None
        return (f"CheckoutResult(status={self.status!r}, task_id={self.task_id!r}, "
                f"repo={self.repo!r}, checkout={ref!r}, held_until={self.held_until!r})")


def checkout_repo_slug(repo: str) -> str:
    """``<name>`` or ``petralabx/<name>`` -> ``petralabx/<name>``; any other owner refused."""
    if not isinstance(repo, str):
        raise ValueError("checkout repo must be a petralabx repository name")
    owner, slash, name = repo.strip().rpartition("/")
    if (slash and owner != CHECKOUT_REPO_OWNER) or not _REPO_NAME.fullmatch(name) or \
            name in (".", ".."):
        raise ValueError("checkout repo must be petralabx/<name>")
    return f"{CHECKOUT_REPO_OWNER}/{name}"


def _guarded_json_get(hub: HubTransport) -> Transport:
    """Default REST transport: guarded (by the caller), retrying, JSON."""
    def get(url: str, headers: Mapping[str, str]) -> Any:
        status, _headers, raw = hub.send("GET", url, headers, None, retry=True)
        if status != 200:
            raise error_from_response(status, raw, attempts=hub.last_attempts, scrub=hub.scrub)
        try:
            return json.loads(raw)
        except ValueError:
            raise McAnswerRefused("response_not_json",
                                  "Mission Control returned non-JSON") from None
    return get


class MissionControlReadClient:
    def __init__(self, api_key: str | None, operator_email: str | None, repo: str, *,
                 transport: Transport | None = None,
                 base_url: str = "https://mc.plxcustomer.io",
                 hub: HubTransport | None = None,
                 audit: McAuditLog | None = None) -> None:
        if not api_key or not operator_email:
            raise SourceUnavailable("Mission Control read credentials are unavailable")
        if not repo or repo.count("/") != 1:
            raise ValueError("Mission Control repo must be owner/repository")
        check_https_base(base_url)
        if (urlsplit(base_url).hostname or "").lower() not in MC_HOSTS:
            raise ValueError("Mission Control base URL host is not one the pack names")
        self._key = Secret(api_key)
        self._operator = operator_email
        self._repo = repo
        self._hub = hub or HubTransport()
        self._hub.register_secret(self._key)
        self._transport = transport or _guarded_json_get(self._hub)
        self._base = base_url.rstrip("/")
        self._host = (urlsplit(self._base).hostname or "").lower()
        self.audit = audit if audit is not None else McAuditLog()

    def __repr__(self) -> str:
        return f"MissionControlReadClient(host={self._host!r}, repo={self._repo!r})"

    def _get(self, route: str, params: list[tuple[str, str]] | None = None) -> Mapping[str, Any]:
        url = self._base + "/api/cursor" + route + ("?" + urlencode(params) if params else "")
        label = "GET /api/cursor" + ("/tasks/{id}" if route.startswith("/tasks/") else route)
        arguments: dict[str, Any] = {}
        for key, value in params or []:
            arguments.setdefault(key, []).append(value)
        if route.startswith("/tasks/"):
            arguments["id"] = route[len("/tasks/"):]
        try:
            check_mc_request("GET", url, mc_host=self._host, environ=self._hub.environ)
            response = self._transport(url, {
                "X-API-Key": self._key.reveal(),
                "X-MC-Operator-Email": self._operator,
                "X-MC-Repo": self._repo,
                "X-MC-Runtime": "plx-operating-platform-read",
            })
        except BaseException as error:
            outcome, code = _outcome(error)
            self.audit.record(door="rest", tool=label, kind="read", arguments=arguments,
                              outcome=outcome, error_code=code,
                              attempts=getattr(error, "attempts", 0))
            raise
        self.audit.record(door="rest", tool=label, kind="read", arguments=arguments,
                          outcome="ok", attempts=max(1, self._hub.last_attempts))
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict) or not isinstance(response.get("meta"), dict):
            raise McAnswerRefused("envelope_invalid",
                                  "Mission Control response is not a {data, meta} envelope")
        actor = response["meta"].get("actor")
        if not isinstance(actor, dict) or actor.get("repo") != self._repo:
            raise McAnswerRefused("actor_unbound", "Mission Control response actor is "
                                  "missing or bound to another repository")
        return response

    def self_check(self) -> Mapping[str, Any]:
        envelope = self._get("/self-check")
        if not isinstance(envelope["data"].get("ok"), bool):
            raise McAnswerRefused("self_check_invalid", "Mission Control self-check lacks ok")
        return envelope

    def search_tasks(self, *, query: str = "", bucket: str | None = None,
                     stage: str | None = None, limit: int = 50) -> Mapping[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("Mission Control task limit must be 1..200")
        params = [("q", query), ("limit", str(limit))]
        if bucket:
            params.append(("bucket", bucket))
        if stage:
            params.append(("stage", stage))
        envelope = self._get("/tasks", params)
        if not isinstance(envelope["data"].get("tasks"), list) or not isinstance(envelope["data"].get("total"), int):
            raise McAnswerRefused("search_tasks_shape_invalid",
                                  "Mission Control task search lacks tasks/total")
        return envelope

    def context(self, *, task_ids: list[str] | None = None, bucket: str | None = None,
                full: bool = False) -> Mapping[str, Any]:
        params = [("depth", "full" if full else "compact")]
        if bucket:
            params.append(("bucket", bucket))
        for task_id in task_ids or []:
            if not isinstance(task_id, str) or not task_id.startswith("TASK-"):
                raise ValueError("context task IDs must be TASK-* identifiers")
            params.append(("taskIds", task_id))
        envelope = self._get("/context", params)
        key = "tasks" if full else "topTasks"
        if not isinstance(envelope["data"].get(key), list):
            raise McAnswerRefused("context_lacks_tasks" if full else "context_lacks_top_tasks",
                                  f"Mission Control context lacks {key}")
        return envelope

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        """Read MC's exact task, evidence and redacted checkout references."""
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("MC task ID must be TASK-*")
        envelope = self._get("/tasks/" + task_id)
        data = envelope["data"]
        task = data.get("task")
        if (data.get("taskId") != task_id or not isinstance(task, dict) or
                task.get("id") != task_id or
                not isinstance(data.get("events"), list) or
                not isinstance(data.get("checkouts"), list)):
            raise McAnswerRefused("task_answer_invalid",
                                  "Mission Control exact task response invalid")
        # MC read-actions intentionally returns only checkoutRef. Refuse a
        # source regression that would expose a usable checkout credential.
        if _USABLE_CHECKOUT.search(json.dumps(data)):
            raise McAnswerRefused("checkout_credential_leaked",
                                  "Mission Control read leaked a checkout credential")
        return envelope


class MissionControlMcpClient:
    """Guarded MCP tool client for ``POST /api/cursor/mcp`` (pack 04 §8 R2).

    Stateless Streamable HTTP: each POST stands alone. Read tools are retried
    on 429/502/503/504. Writes are refused unless opted in (see the module
    docstring); ``write_tools`` and ``write_approvers`` default to empty.
    """

    def __init__(self, config: McConfig, *, hub: HubTransport | None = None,
                 audit: McAuditLog | None = None,
                 write_tools: Iterable[str] = (),
                 write_approvers: Iterable[str] = (),
                 write_ledger: WriteAuthorizationLedger | None = None) -> None:
        if not isinstance(config, McConfig):
            raise TypeError("config must be an McConfig")
        if isinstance(write_tools, str) or isinstance(write_approvers, str):
            raise TypeError("write_tools and write_approvers must be collections")
        tools = frozenset(write_tools)
        if tools - frozenset(MC_APPROVABLE_WRITE_TOOLS):
            raise ValueError("only the pack's W-A write tools can be opted in: "
                             + ", ".join(sorted(tools - frozenset(MC_APPROVABLE_WRITE_TOOLS))))
        approvers = list(write_approvers)
        if any(not is_human_email(a) for a in approvers):
            raise ValueError("write approvers must be human emails")
        approvers = frozenset(normalize_email(a) for a in approvers)
        if normalize_email(config.operator_email) in approvers:
            # Separation of duties, as MC's approvals.ts:69-106 (plus-aliases too).
            raise ValueError("the MC operator cannot approve its own writes")
        if tools and not approvers:
            raise ValueError("write_tools need at least one write approver")
        if tools and not isinstance(write_ledger, WriteAuthorizationLedger):
            raise ValueError("write_tools need a durable WriteAuthorizationLedger")
        self._config = config
        self._hub = hub or HubTransport()
        self._hub.register_secret(config.key)
        self._ids = itertools.count(1)
        self._write_tools = tools
        self._write_approvers = approvers
        self._ledger = write_ledger
        self.audit = audit if audit is not None else McAuditLog()

    @property
    def write_tools(self) -> frozenset[str]:
        return self._write_tools

    def __repr__(self) -> str:
        return f"MissionControlMcpClient({self._config!r})"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "x-api-key": self._config.key.reveal(),
                "x-mc-operator-email": self._config.operator_email,
                "x-mc-repo": self._config.repo,
                "x-mc-runtime": self._config.runtime}

    def _rpc(self, method: str, params: Mapping[str, Any], *,
             authorization: McWriteAuthorization | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": dict(params)}
        url = self._config.base_url.rstrip("/") + "/api/cursor/mcp"
        check_mc_request("POST", url, mc_host=self._config.host, body=body,
                         environ=self._hub.environ, write_authorization=authorization,
                         now=self._hub.now() if authorization is not None else None)
        status, headers, raw = self._hub.send(
            "POST", url, self._headers(), json.dumps(body).encode(),
            retry=authorization is None)
        if status != 200:
            raise error_from_response(status, raw, attempts=self._hub.last_attempts,
                                      scrub=self._hub.scrub)
        message = _jsonrpc_message(raw, headers, body["id"])
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise UpstreamError(self._hub.scrub(
                f"MC JSON-RPC error {error.get('code', '?')}"), code="upstream_error",
                status=status, attempts=self._hub.last_attempts, layer="jsonrpc",
                rpc_code=error.get("code"))
        if not isinstance(message.get("result"), dict):
            raise McAnswerRefused("jsonrpc_lacks_result", "MC JSON-RPC response lacks result")
        return message["result"]

    def list_tool_specs(self) -> list[dict[str, Any]]:
        """``tools/list``: every named tool as MC lists it (``inputSchema``, and
        ``outputSchema`` when declared), by name. A paged answer is refused: the rest of
        the list would be missing silently."""
        result = self._rpc("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McAnswerRefused("tools_list_invalid", "MC tools/list lacks tools")
        if result.get("nextCursor"):
            raise McAnswerRefused("tools_list_paged",
                                  "MC tools/list is paged; only the first page was read")
        return sorted((t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)),
                      key=lambda t: t["name"])

    def list_tools(self) -> list[str]:
        return [t["name"] for t in self.list_tool_specs()]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Call one read tool; the guard refuses every write before I/O."""
        return self._audited(name, dict(arguments or {}), None)

    def call_write_tool(self, name: str, arguments: Mapping[str, Any], *,
                        authorization: McWriteAuthorization) -> Any:
        """Send one opted-in, human-authorized W-A write, once.

        Refused before I/O unless ``name`` is in ``write_tools``, the
        authorization names exactly this call, its approver is a configured
        ``write_approvers`` human, it is inside its window and unused in the
        durable ledger. The authorization is recorded in the ledger before
        I/O, so a failed or lost call is
        never re-sent with it: an ``UpstreamError`` other than an MC refusal
        means the outcome is unknown and must be read back, not retried.
        """
        args = dict(arguments)
        try:
            if name not in self._write_tools:
                raise ReadOnlyGuardError(f"MC write {name[:60]} is not opted in on this client")
            if not isinstance(authorization, McWriteAuthorization):
                raise ReadOnlyGuardError("MC write needs an McWriteAuthorization")
            if normalize_email(authorization.approved_by) not in self._write_approvers:
                raise ReadOnlyGuardError("MC write approver is not a configured write approver")
            # Validate the exact call (tool, arguments, window) before consuming.
            check_mc_request("POST", self._config.base_url.rstrip("/") + "/api/cursor/mcp",
                             mc_host=self._config.host,
                             body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}},
                             environ=self._hub.environ, write_authorization=authorization,
                             now=self._hub.now())
            assert self._ledger is not None  # constructor requires it with write_tools
            self._ledger.consume(authorization, at=self._hub.now())
        except ReadOnlyGuardError as error:
            self.audit.record(door="mcp", tool=name if isinstance(name, str) else "?",
                              kind="write", arguments=args, outcome="refused",
                              error_code=_outcome(error)[1],
                              authorization=authorization
                              if isinstance(authorization, McWriteAuthorization) else None)
            raise
        return self._audited(name, args, authorization)

    def _audited(self, name: str, args: dict[str, Any],
                 authorization: McWriteAuthorization | None) -> Any:
        kind = "read" if authorization is None and name in MC_READ_TOOLS else "write"
        label = name if isinstance(name, str) else "?"
        self._hub.last_attempts = 0
        try:
            payload = self._call(name, args, authorization)
        except BaseException as error:
            outcome, code = _outcome(error)
            self.audit.record(door="mcp", tool=label, kind=kind, arguments=args,
                              outcome=outcome, error_code=code,
                              attempts=self._hub.last_attempts, authorization=authorization)
            raise
        self.audit.record(door="mcp", tool=label, kind=kind, arguments=args, outcome="ok",
                          attempts=self._hub.last_attempts, authorization=authorization)
        return payload

    def _call(self, name: str, args: Mapping[str, Any],
              authorization: McWriteAuthorization | None) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": dict(args)},
                           authorization=authorization)
        content = result.get("content")
        if (not isinstance(content, list) or not content or not isinstance(content[0], dict)
                or not isinstance(content[0].get("text"), str)):
            raise McAnswerRefused("tool_result_lacks_text", "MC tool result lacks content[0].text")
        try:
            payload = json.loads(content[0]["text"])
        except ValueError:
            raise McAnswerRefused("tool_result_not_json",
                                  "MC tool result text is not JSON") from None
        if result.get("isError"):
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            code = code[:80] if isinstance(code, str) else "tool_error"
            if code == "checkout_held":
                text = error.get("message") if isinstance(error, dict) else None
                raise CheckoutHeld(f"MC tool {name} returned error {code}",
                                   upstream_message=self._hub.scrub(text[:300])
                                   if isinstance(text, str) else None)
            raise UpstreamError(f"MC tool {name} returned error {code}", code=code, status=200,
                                attempts=self._hub.last_attempts, layer="tool")
        if not isinstance(payload, dict):
            raise McAnswerRefused("tool_result_not_object", "MC tool result is not an object")
        return payload

    # -- read tools (pack 04 §3 rows 1-12) --------------------------------

    def self_check(self) -> Mapping[str, Any]:
        payload = self.call_tool("mc_self_check")
        if not isinstance(payload.get("ok"), bool):
            raise McAnswerRefused("self_check_invalid", "mc_self_check lacks ok")
        return payload

    def get_context(self, *, depth: str = "compact", bucket: str | None = None,
                    task_ids: list[str] | None = None) -> Mapping[str, Any]:
        """``mc_get_context``: replaces the retired VMC ``/api/vmc/cursor/context``."""
        args: dict[str, Any] = {"depth": depth}
        if bucket is not None:
            args["bucket"] = bucket
        if task_ids:
            args["taskIds"] = list(task_ids)
        payload = self.call_tool("mc_get_context", args)
        data = payload.get("data")
        key = "tasks" if depth == "full" else "topTasks"
        if not isinstance(data, dict) or not isinstance(data.get(key), list):
            raise McAnswerRefused("context_lacks_tasks" if depth == "full"
                                  else "context_lacks_top_tasks",
                                  f"mc_get_context lacks data.{key}")
        return payload

    def search_tasks(self, query: str = "", *, limit: int = 20,
                     cursor: str | None = None) -> Mapping[str, Any]:
        """``mc_search_tasks``: one keyset page, ``{data: {tasks, total, nextCursor}}``
        (PLX_MC@77a5af6 create-http-server.ts:188-205, sync/task-search.ts
        ``searchTaskPage``); ``cursor`` is the previous page's ``nextCursor``."""
        args: dict[str, Any] = {"q": query, "limit": limit}
        if cursor is not None:
            args["cursor"] = cursor
        payload = self.call_tool("mc_search_tasks", args)
        data = payload.get("data")
        total = data.get("total") if isinstance(data, dict) else None
        after = data.get("nextCursor") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) \
                or isinstance(total, bool) or not isinstance(total, int) or total < 0 \
                or not (after is None or isinstance(after, str) and 1 <= len(after) <= 4096):
            raise McAnswerRefused("search_tasks_shape_invalid",
                                  "mc_search_tasks lacks data.tasks/total/nextCursor")
        return payload

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        payload = self.call_tool("mc_get_task", {"id": task_id})
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("task"), dict) or \
                data["task"].get("id") != task_id:
            raise McAnswerRefused("task_answer_invalid", "mc_get_task response invalid")
        if _USABLE_CHECKOUT.search(json.dumps(data)):
            raise McAnswerRefused("checkout_credential_leaked",
                                  "Mission Control read leaked a checkout credential")
        return payload

    def search_knowledge(self, query: str, *, limit: int = 8) -> Mapping[str, Any]:
        """``mc_search_knowledge``. A non-``ok`` status is unavailable, never
        empty (pack 04 §5); cold-start ``upstream_error`` is retried with the
        same bounded backoff. Hits are untrusted leads (T3), never business
        facts."""
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("knowledge query must be 1..200 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
            raise ValueError("knowledge limit must be 1..25")
        for attempt in range(1 + self._hub.max_retries):
            payload = self.call_tool("mc_search_knowledge", {"q": query.strip(), "limit": limit})
            data = payload.get("data", payload)
            status = data.get("status") if isinstance(data, dict) else None
            if status == "ok":
                if not isinstance(data.get("hits"), list):
                    raise McAnswerRefused("knowledge_lacks_hits", "mc_search_knowledge lacks hits")
                return payload
            if status in KNOWLEDGE_RETRY_STATUSES and attempt < self._hub.max_retries:
                self._hub.pause(attempt)
                continue
            # MC answered HTTP 200 and the tool succeeded; MC keeps the brain's own
            # status to itself (PLX_MC src/lib/brain-ask/status.ts: upstream_error is a
            # non-2xx or non-JSON brain answer), so the layer is what we can name.
            code = status if isinstance(status, str) else "upstream_error"
            raise UpstreamError(f"mc_search_knowledge status {code[:40]}", code=code[:40],
                                status=200, attempts=attempt + 1, layer="knowledge_status")
        raise AssertionError("unreachable")  # pragma: no cover

    # -- checkout (a W-A write) -------------------------------------------

    def checkout_task(self, task_id: str, repo: str, *,
                      authorization: McWriteAuthorization) -> CheckoutResult:
        """``mc_checkout_task{taskId, repo:"petralabx/<name>"}`` (pack 04 §4).

        ``repo`` may be ``<name>`` or ``petralabx/<name>``; the call always
        carries the full slug (and the authorization must name it). A 409
        ``checkout_held`` becomes ``CheckoutResult(status="checkout_held")``.
        A stamp is accepted only when MC returns our ``taskId``, our repo in
        ``actor.repo`` and ``prBodyLine == "MC-Checkout: " + checkoutId``;
        otherwise ``SourceUnavailable`` (do not use that stamp).
        """
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ValueError("MC task ID must be TASK-*")
        slug = checkout_repo_slug(repo)
        try:
            payload = self.call_write_tool("mc_checkout_task", {"taskId": task_id, "repo": slug},
                                           authorization=authorization)
        except CheckoutHeld as held:
            text = held.upstream_message
            match = _HELD_UNTIL.search(text) if isinstance(text, str) else None
            return CheckoutResult("checkout_held", task_id, slug,
                                  held_until=match.group(1) if match else None,
                                  message=text)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        checkout_id = data.get("checkoutId")
        actor = data.get("actor")
        mc_repo = actor.get("repo") if isinstance(actor, dict) else None
        # MC returns its allowlist spelling (checkout-repo.ts resolveCheckoutRepo
        # matches case-insensitively); same repo, MC's canonical case kept.
        if (data.get("taskId") != task_id or not isinstance(mc_repo, str)
                or mc_repo.lower() != slug.lower() or not isinstance(checkout_id, str)
                or not _USABLE_CHECKOUT.fullmatch(checkout_id)
                or data.get("prBodyLine") != "MC-Checkout: " + checkout_id):
            raise McAnswerRefused("checkout_answer_unbound", "MC checkout answer is not bound to "
                                  "the requested task and repo; do not use that stamp")
        return CheckoutResult("checked_out", task_id, mc_repo, checkout_id=Secret(checkout_id))

    # -- approval gates: read-only re-check of MC's decision -------------

    def approval_decision(self, task_id: str, gate_id: str, *,
                          expected_reason: str) -> dict[str, Any]:
        """Read one gate and decide whether it authorizes *our* expected action.

        ``authorizes`` is True only when every check holds; otherwise
        ``reason_code`` names the first failing check:
        ``not_approved``, ``foreign_requester`` (requestedBy is not our
        operator), ``reason_mismatch`` (gate reason differs from the exact
        action the caller expects; compared by SHA-256), ``decider_missing``,
        ``decider_not_human`` (service principal / agent label / non-email),
        ``self_decided`` (decider is our operator or the requester),
        ``decided_at_missing`` or ``decided_before_request``.
        """
        if not isinstance(gate_id, str) or not _GATE_ID.fullmatch(gate_id):
            raise ValueError("gate id must be apg_*")
        if not isinstance(expected_reason, str) or not 1 <= len(expected_reason.strip()) <= 500:
            raise ValueError("expected_reason must be the exact 1..500 character gate reason")
        task = self.get_task(task_id)["data"]["task"]
        gates = [g for g in task.get("approvalGates") or [] if isinstance(g, dict)
                 and g.get("id") == gate_id]
        if len(gates) != 1:
            raise McAnswerRefused("approval_gate_not_unique",
                                  "approval gate not found exactly once on the task")
        gate = gates[0]

        def text(key: str) -> str:
            value = gate.get(key)
            return value.strip() if isinstance(value, str) else ""

        status, requested_by, decided_by = text("status"), text("requestedBy"), text("decidedBy")
        operator = self._config.operator_email.strip().lower()
        gate_reason = gate.get("reason") if isinstance(gate.get("reason"), str) else ""
        reason_hash = hashlib.sha256(gate_reason.encode()).hexdigest()
        expected_hash = hashlib.sha256(expected_reason.encode()).hexdigest()
        decided_at = _parse_time(gate.get("decidedAt"))
        requested_at = _parse_time(gate.get("requestedAt"))
        if status != "approved":
            code = "not_approved"
        elif requested_by.lower() != operator:
            code = "foreign_requester"
        elif not hmac.compare_digest(reason_hash, expected_hash):
            code = "reason_mismatch"
        elif not decided_by:
            code = "decider_missing"
        elif _SERVICE_LABEL.match(decided_by) or not _HUMAN_EMAIL.fullmatch(decided_by):
            code = "decider_not_human"
        elif decided_by.lower() in {operator, requested_by.lower()}:
            code = "self_decided"
        elif decided_at is None:
            code = "decided_at_missing"
        elif requested_at is not None and decided_at < requested_at:
            code = "decided_before_request"
        else:
            code = None
        return {"task_id": task_id, "gate_id": gate_id, "status": status or None,
                "decided_by": decided_by or None,
                "decided_at": gate.get("decidedAt") if decided_at else None,
                "reason_sha256": reason_hash, "authorizes": code is None,
                "reason_code": code or "authorized"}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _jsonrpc_message(raw: bytes, headers: Mapping[str, str], request_id: int) -> dict[str, Any]:
    """Parse a JSON or SSE (``data:`` lines) JSON-RPC answer for one id."""
    content_type = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    candidates: list[Any] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise McAnswerRefused("response_not_utf8", "MC response is not UTF-8") from None
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    candidates.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        try:
            candidates.append(json.loads(text))
        except ValueError:
            raise McAnswerRefused("response_not_json", "MC response is not JSON") from None
    for message in candidates:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McAnswerRefused("jsonrpc_answer_missing", "MC response lacks the JSON-RPC answer")
