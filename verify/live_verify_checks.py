"""Judging one live check: expectations, blocked and upstream causes, the result envelope and
traceback errors (``verify.live_verify``, which re-exports them)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
from stat import S_ISDIR as stat_is_dir
import subprocess
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from verify.live_verify_policy import (
    CADENCES,
    CAUSES,
    CHECK_DIR,
    CHECK_PREFIX,
    DAILY_REPORT_ARG,
    DEFAULT_CADENCE,
    ENUM_STRING,
    ENVELOPE_DISK,
    ENVELOPE_KEYS,
    ENVELOPE_STRING,
    ERROR_CODE,
    EXCEPTION_CLASS,
    EXPECT_KEYS,
    MAX_BLOCKED_PARTS,
    MAX_ITEMS,
    REASON_TEXT,
    RELATION_UNAVAILABLE_CODES,
    REPO_FRAME_PATH,
    SESSION_ARG,
    SESSION_DIR,
    SESSION_PREFIX,
    SESSION_STATE,
    TRACEBACK_FRAME,
    TRACEBACK_HEADER,
)
from verify.live_verify_specs import _is_number, _same, resolve, summarize


# ------------------------------------------------------------------ running

DAILY_STALE_HOURS = 26


SET_CADENCES = ("daily", "weekly")  # the cadences the daily set holds


def weekly_of(entries: Iterable[Mapping[str, Any]]) -> list[str]:
    """The ids of the valid specs that declare the ``weekly`` cadence."""
    return [e["id"] for e in entries
            if e["spec"] is not None and spec_cadence(e["spec"]) == "weekly"]


def spec_cadence(spec: Mapping[str, Any] | None) -> str:
    """The cadence a (valid) spec declares; ``hourly`` when absent or unreadable."""
    value = spec.get("cadence") if isinstance(spec, Mapping) else None
    return value if value in CADENCES else DEFAULT_CADENCE


def check_expectations(spec: Mapping[str, Any], exit_code: int, document: Any,
                       expect: Mapping[str, Any] | None = None) -> list[str]:
    expect = spec["expect"] if expect is None else expect
    failures = []
    if exit_code != expect["exit_code"]:
        failures.append(f"exit_code {exit_code} != {expect['exit_code']}")
    needs_json = any(expect.get(k) for k in EXPECT_KEYS - {"exit_code"})
    if needs_json and document is None:
        return failures + ["stdout is not a JSON object"]
    for path in expect["json_fields"]:
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif any(value is None for value in found):  # a field the run did not measure
            failures.append(f"null {path}")
    for path, wanted in expect.get("json_equals", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_same(value, wanted) for value in found):
            failures.append(f"{path} != expected")
    for path, minimum in expect.get("json_min", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_is_number(v) and v >= minimum for v in found):
            failures.append(f"{path} < {minimum}")
    for path, maximum in expect.get("json_max", {}).items():
        found = resolve(document, path)
        if not found:
            failures.append(f"missing {path}")
        elif not all(_is_number(v) and v <= maximum for v in found):
            failures.append(f"{path} > {maximum}")
    for path, other in expect.get("json_same", {}).items():
        left, right = resolve(document, path), resolve(document, other)
        if not left or not right:
            failures.append(f"missing {path if not left else other}")
        elif left[0] is None or not _same(left[0], right[0]):
            failures.append(f"{path} != {other}")
    for path in expect.get("json_empty", []):
        if not all(v is None or v == [] or v == {} for v in resolve(document, path)):
            failures.append(f"{path} not empty")
    for path in expect.get("json_absent", []):
        if resolve(document, path):
            failures.append(f"{path} present")
    return failures


def blocked_cause(http_status: Any = None, error_code: Any = None, declared: Any = None) -> str:
    """One of ``CAUSES``: a cause the command declared, else ``relation_unavailable`` for
    the service's ``relation-unavailable`` code, else ``host_misconfiguration`` for
    a refused credential or route (401/403/407, ``not_configured``), else
    ``upstream_outage`` (5xx, throttling, transport, timeout)."""
    if declared in CAUSES:
        return declared
    if error_code in RELATION_UNAVAILABLE_CODES:
        return "relation_unavailable"
    if (isinstance(http_status, int) and http_status in (401, 403, 407)) or \
            error_code in ("not_configured", "unauthorized", "forbidden"):
        return "host_misconfiguration"
    return "upstream_outage"


def blocked_parts(document: Any) -> list[dict[str, Any]]:
    """The command's ``blocked_upstream`` parts, rebuilt from names, statuses and codes
    only (``[]`` when absent or empty); each carries its ``cause``."""
    parts = document.get("blocked_upstream") if isinstance(document, Mapping) else None
    out = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, Mapping):
            continue
        status, code = part.get("http_status"), part.get("error_code")
        entry: dict[str, Any] = {
            key: part[key] for key in ("part", "source")
            if isinstance(part.get(key), str) and ENUM_STRING.fullmatch(part[key])}
        if isinstance(status, int) and not isinstance(status, bool):
            entry["http_status"] = status
        if isinstance(code, str) and ERROR_CODE.fullmatch(code):
            entry["error_code"] = code
        entry["cause"] = blocked_cause(entry.get("http_status"), entry.get("error_code"),
                                       part.get("cause"))
        reason = part.get("reason")
        if entry["cause"] == "relation_unavailable" and isinstance(reason, str) and REASON_TEXT.fullmatch(reason):
            entry["reason"] = reason
        out.append(entry)
    return out[:MAX_BLOCKED_PARTS]


def document_causes(document: Any) -> list[str]:
    """The distinct enum-safe ``cause`` values of the command's top-level ``causes`` list
    (``[{part, cause}]``, e.g. ``credential_refused`` for a refused key), in order."""
    causes = document.get("causes") if isinstance(document, Mapping) else None
    found = [c.get("cause") for c in causes if isinstance(c, Mapping)] \
        if isinstance(causes, list) else []
    return list(dict.fromkeys(c for c in found if isinstance(c, str) and c
                              and ENUM_STRING.fullmatch(c)))[:MAX_BLOCKED_PARTS]


def completeness(spec: Mapping[str, Any], document: Any) -> dict[str, Any]:
    """``complete_when`` over the command's output: ``complete`` and the paths that are not
    (missing or another value); ``{}`` for a spec without it."""
    wanted = spec.get("complete_when")
    if not wanted:
        return {}
    if document is None:
        return {"complete": False, "incomplete": sorted(wanted)}
    missing = [path for path, value in wanted.items()
               if not (found := resolve(document, path)) or
               not all(_same(v, value) for v in found)]
    return {"complete": not missing, **({"incomplete": missing} if missing else {})}


def record_paths(spec: Mapping[str, Any]) -> list[str]:
    """The paths a check records: ``record``, else every expected path."""
    expect = spec["expect"]
    return list(spec.get("record") or dict.fromkeys(
        [*expect["json_fields"], *expect.get("json_equals", {}), *expect.get("json_min", {}),
         *expect.get("json_max", {}), *expect.get("json_same", {}),
         *expect.get("json_same", {}).values(),
         *expect.get("json_empty", []), *expect.get("json_absent", [])]))


def under(path: str, prefixes: Sequence[str]) -> bool:
    """``path`` is one of ``prefixes`` or below one: how a recorded path takes a
    ``record_enums`` / ``record_keys`` entry (an entry below a recorded path has no
    effect)."""
    return any(path == e or path.startswith(e + ".") or path.startswith(e + "[")
               for e in prefixes)


def _recorded(spec: Mapping[str, Any], document: Any) -> dict[str, Any]:
    paths = record_paths(spec)
    enum_paths = spec.get("record_enums", [])
    key_paths = spec.get("record_keys", [])

    fields = _envelope(document)
    for path in paths:
        enums, keys = under(path, enum_paths), under(path, key_paths)
        if path in fields and not (enums or keys):  # the spec's wider view wins
            continue
        found = resolve(document, path)
        if not found:
            continue
        values = [summarize(v, enums=enums, keys=keys) for v in found[:MAX_ITEMS]]
        wildcard = "[*]" in path
        fields[path] = values if wildcard else values[0]
        if wildcard and len(found) > MAX_ITEMS:
            fields[path + ".count"] = len(found)
    return fields


def _envelope(document: Any) -> dict[str, Any]:
    """The ``ENVELOPE_KEYS`` the command printed at top level: numbers, booleans and null
    as they are, an identifier-shaped string as it is, anything else typed (a long or
    free-text string by its length, a list by its count, a mapping by its key count)."""
    out: dict[str, Any] = {}
    for key in ENVELOPE_KEYS:
        if not isinstance(document, Mapping) or key not in document:
            continue
        value = document[key]
        if isinstance(value, str):
            out[key] = value if ENVELOPE_STRING.fullmatch(value) else f"<str len={len(value)}>"
        else:  # depth 3: a list or mapping is reduced to its size, never its items
            out[key] = summarize(value, enums=False, depth=3)
    if not isinstance(document, Mapping):
        return out
    store = document.get("store")
    if isinstance(store, str) and ENVELOPE_STRING.fullmatch(store):
        out["store"] = store
    disk = _envelope_disk(document.get("disk"))
    if disk:
        out["disk"] = disk
    return out


def _envelope_disk(value: Any) -> dict[str, dict[str, int]]:
    """``ENVELOPE_DISK`` counts of a stop line's ``disk``: non-negative integers only."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, dict[str, int]] = {}
    for name, counts in ENVELOPE_DISK.items():
        measured = value.get(name)
        if not isinstance(measured, Mapping):
            continue
        kept = {k: measured[k] for k in counts
                if type(measured.get(k)) is int and measured[k] >= 0}
        if kept:
            out[name] = kept
    return out


def _traceback_error(lines: Sequence[str], root: Path) -> dict[str, str] | None:
    """The last traceback of ``lines`` (raw stderr lines): ``error_class``, the class token
    of its exception line, whatever the class is named and however many lines its message
    has (never the message), and ``frame``, its innermost frame in a file of the
    repository at ``root`` as ``path:line``. The exception line is the first line after
    the frames that is ``Class: message``; without one, a bare ``Class`` (a capitalised
    last name part) only when it is the one bare word there, since a bare word may be
    another thread's print. None without a traceback; no exception line is
    ``stderr_unparsed`` beside the frame."""
    starts = [i for i, line in enumerate(lines) if line.rstrip() == TRACEBACK_HEADER]
    if not starts:
        return None
    error_class = repo_frame = None
    bare: list[str] = []
    for line in lines[starts[-1] + 1:]:
        frame = TRACEBACK_FRAME.match(line)
        if frame is not None:
            path = _repo_frame_path(frame.group(1), root)
            if path is not None:
                repo_frame = f"{path}:{frame.group(2)}"
            continue
        if not line.strip() or line[:1].isspace():   # a frame's source and caret lines
            continue
        token, colon, _ = line.partition(":")
        token = token.strip()
        if len(token) > 80 or not EXCEPTION_CLASS.fullmatch(token):
            break   # neither an exception line nor a bare word: never read further
        if colon:
            error_class = token
            break
        bare.append(token)
    if error_class is None:
        names = [token for token in bare if token.rsplit(".", 1)[-1][:1].isupper()]
        error_class = names[0] if len(names) == 1 else None
    return {**({"error_class": error_class} if error_class else {"error": "stderr_unparsed"}),
            **({"frame": repo_frame} if repo_frame else {})}


def _repo_frame_path(path: str, root: Path) -> str | None:
    """``path`` relative to the repository ``root`` when it names a source file there (an
    installed package or a frozen module is no repository frame), else None."""
    if path.startswith("<"):
        return None
    try:
        relative = (root / path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if not REPO_FRAME_PATH.fullmatch(relative) or len(relative) > 160 or any(
            part in ("site-packages", "dist-packages") for part in relative.split("/")):
        return None
    return relative


def _command_text(command: Sequence[str]) -> str:
    return " ".join(arg.replace("{tmp}", "<tmp>").replace("{daily_report}", "<daily_report>")
                    .replace(SESSION_STATE, "<session_state>") for arg in command)


def _expand(args: Sequence[str], tmp: str, daily_report: Path | None,
            session: Mapping[str, str] | None = None) -> list[str]:
    """Fill the placeholders; ``--previous-report={daily_report}`` is dropped without a
    readable daily report file (the probe then has no previous run). ``session``: the
    directory each ``{session_state}`` path expands in (``session_paths`` keys); without
    it every one expands in ``tmp``."""
    have = daily_report is not None and Path(daily_report).is_file()
    out = []
    for arg in args:
        if not have and arg == DAILY_REPORT_ARG:
            continue
        found = SESSION_ARG.search(arg)
        base = (session or {}).get(found.group(1), tmp) if found else tmp
        out.append(arg.replace("{tmp}", tmp).replace("{daily_report}", str(daily_report))
                   .replace(SESSION_STATE, base))
    return out


def tree_bytes(path: str | Path) -> int:
    """The bytes the files under ``path`` take on disk (allocated blocks; links not
    followed); 0 for what cannot be read."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                continue
    return total


def free_bytes(directory: str | Path) -> int | None:
    """Bytes available to this user on the file system holding ``directory``."""
    try:
        st = os.statvfs(directory)
    except (OSError, AttributeError):
        return None
    return st.f_bavail * st.f_frsize


def sweep_leftovers(directory: str | Path | None = None) -> dict[str, Any]:
    """Count the ``live-verify-*`` directories in the temp directory (this user's, never a
    link) and their bytes, then remove those a verifier process no longer alive named for
    its pid (``SESSION_DIR`` or ``CHECK_DIR``, mode 0700), the ones a killed run left
    behind. Counts only: ``free_bytes_start`` (before removing), ``leftover_dirs``,
    ``leftover_bytes``, ``swept_dirs``, ``swept_bytes`` and ``session_dirs_swept`` (the
    session directories among them). A live pid keeps its directory (another run, or a
    reused pid); a directory named for no pid is counted and kept."""
    base = Path(directory if directory is not None else tempfile.gettempdir())
    out = {"free_bytes_start": free_bytes(base), "leftover_dirs": 0, "leftover_bytes": 0,
           "swept_dirs": 0, "swept_bytes": 0, "session_dirs_swept": 0}
    for entry in sorted(base.glob(CHECK_PREFIX + "*")):
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat_is_dir(info.st_mode) or info.st_uid != os.getuid():
            continue
        size = tree_bytes(entry)
        out["leftover_dirs"] += 1
        out["leftover_bytes"] += size
        match = SESSION_DIR.fullmatch(entry.name) or CHECK_DIR.fullmatch(entry.name)
        if match is None or info.st_mode & 0o777 != 0o700 or _alive(int(match.group(1))):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            out["swept_dirs"] += 1
            out["swept_bytes"] += size
            out["session_dirs_swept"] += entry.name.startswith(SESSION_PREFIX)
    return out


def _alive(pid: int) -> bool:
    """A process with this pid exists (this one, another user's or one not to judge)."""
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True
    return True


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        process.kill()


def unmet_env(spec: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """The skip detail of a spec whose ``requires_env`` / ``requires_env_any`` is unmet
    (names only), else empty. One rule for the run and for the budget deferral."""
    missing = [name for name in spec.get("requires_env", []) if not env.get(name)]
    any_of = spec.get("requires_env_any", [])
    detail = {"missing_env": missing} if missing else {}
    if any_of and not any(env.get(name) for name in any_of):
        detail["missing_env_any"] = list(any_of)
    return detail


def absent_credentials(spec: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """The ``blocked_without_env`` names unset in ``env`` (names only, never a value)."""
    return [name for name in spec.get("blocked_without_env", []) if not env.get(name)]


def _keep_state_dir(argv: Sequence[str], tmp: str, keep: Path) -> None:
    """Move the directory ``--state-dir`` names in ``argv`` out of ``tmp`` to ``keep``."""
    for flag, value in zip(argv, argv[1:]):
        source = Path(value)
        if flag == "--state-dir" and source.is_dir() and source.resolve().is_relative_to(
                Path(tmp).resolve()):
            shutil.rmtree(keep, ignore_errors=True)
            shutil.move(str(source), str(keep))
            return


def upstream_cause(check: Mapping[str, Any]) -> str:
    """The ``CAUSES`` entry for a dependent of a failed or blocked upstream check."""
    detail = check.get("detail") or {}
    if isinstance(detail.get("cause"), str) and detail["cause"] in CAUSES:
        return detail["cause"]
    blocked = detail.get("blocked") if isinstance(detail.get("blocked"), Mapping) else {}
    if blocked.get("cause") in CAUSES:
        return blocked["cause"]
    stderr = detail.get("stderr") if isinstance(detail.get("stderr"), Mapping) else {}
    return blocked_cause(stderr.get("http_status"), stderr.get("error_code"))


def upstream_reason(check: Mapping[str, Any]) -> str:
    """``upstream <source> <http_status/error_code>`` for a failed upstream check: only the
    status, code or error class the check recorded, else ``failed``/``timeout``."""
    detail = check.get("detail") or {}
    stderr = detail.get("stderr") if isinstance(detail.get("stderr"), Mapping) else {}
    parts = [str(stderr[k]) for k in ("http_status", "error_code", "error", "error_class")
             if stderr.get(k) is not None]
    if not parts:
        parts = ["timeout" if detail.get("timed_out") else check.get("result") or "failed"]
    return f"upstream {check.get('source') or 'n/a'} {' '.join(parts[:2])}"
