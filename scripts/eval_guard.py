"""Every eval and harness entry point calls ``enforce()`` first: an eval may read live
state, never write it (ops-hardening-1117).

11-17: the Ask eval's in-process API overwrote the served ``/data/plx-state/app-sessions.json``
(signed users out) and the assist eval read a retired SQLite generation. ``enforce()`` makes
both impossible for the process and every Python process it starts:

1. **Configured write paths.** Each write-path flag on the command line (``--out``,
   ``--app-data-dir``, ``--session-store``, ... and the same inside ``--api-args``) and each
   write-path variable in the environment is resolved (``realpath``); one under a live root
   (``/data/plx-state``, the API's home ``/var/lib/plx-workbench``, and any in
   ``PLX_EVAL_PROTECT``) refuses the start. An in-process API (``--api-args`` naming
   ``--state-dir``) must name its own ``--app-data-dir`` and ``--session-store``.
2. **Own folders.** ``PLX_EVAL_HOME`` (default ``~/.cache/plx-eval/<entry>``) holds the
   eval's state; ``XDG_STATE_HOME`` (the app's default data folder), ``PLX_FEEDBACK_DIR`` and
   ``PLX_MODEL_LEDGER`` point into it unless already set elsewhere.
3. **Runtime wall.** An audit hook (``sys.addaudithook``) refuses every write-capable open,
   rename, remove, mkdir, link, chmod, truncate, copy target and non-``mode=ro`` SQLite
   connect under a live root, and any SQLite open of the retired generation folder
   (``<root>/projection``). Python children inherit it: ``PLX_EVAL_GUARD=1`` plus the
   ``scripts/eval_guard_site`` folder on ``PYTHONPATH`` (its ``sitecustomize`` installs the
   same hook).
4. **Read-only Postgres.** ``PGUSER=plx_eval`` (``deploy/pg/plx_eval_role.sql``: SELECT only,
   ``default_transaction_read_only=on``); a DSN naming another user or a password refuses.
   When the process reads Postgres (``PLX_ONTOLOGY_PG`` on, or a DSN given), one probe
   checks the login is that role, no superuser, read-only by default; anything else
   (including no connection) refuses.

A refusal exits 2 with one line on stderr (``eval_guard: refused ...``). Nothing here prints
a DSN's secret part or any environment value but paths.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

LIVE_ROOTS = ("/data/plx-state", "/var/lib/plx-workbench")
RETIRED_SQLITE = ("projection",)          # the retired SQLite generation, under a live root
EVAL_ROLE = "plx_eval"
MARK = "PLX_EVAL_GUARD"
ROOTS_ENV = "PLX_EVAL_GUARD_ROOTS"         # the roots the children's hook walls off (JSON)
SITE_DIR = Path(__file__).resolve().parent / "eval_guard_site"

# flags whose value an eval writes (exact names; ``--x=value`` and ``--x value`` both read)
WRITE_FLAGS = frozenset((
    "--out", "--output", "--out-dir", "--output-dir", "--outdir", "--results", "--result",
    "--report-out", "--work-dir", "--workdir", "--work", "--cache-dir", "--save", "--record",
    "--trace-dir", "--traces", "--log", "--log-dir", "--logs", "--app-data-dir",
    "--session-store", "--feedback-dir", "--write", "--dest", "--target-dir", "--run-dir",
    "--scratch", "--tmp-dir", "--json-out", "--csv-out", "--md-out", "--progress",
    "--checkpoint", "--journal", "--ledger"))
# environment variables naming a write target
WRITE_ENV = re.compile(r"^(PLX_([A-Z0-9_]*_)?(OUT|OUTPUT|OUT_DIR|LEDGER|SESSION_STORE|"
                       r"FEEDBACK_DIR|APP_DATA|APP_DATA_DIR|CACHE_DIR|WORK_DIR|LOG|LOG_DIR|"
                       r"JOURNAL|EVAL_HOME)|XDG_STATE_HOME|XDG_CACHE_HOME|TMPDIR)$")
DSN_ENV = re.compile(r"^(PLX_[A-Z0-9_]*DSN|DATABASE_URL)$")
DSN_FLAGS = ("--pg-dsn", "--dsn")


class Refused(PermissionError):
    """An eval tried to start (or write) where it may not."""


# --- paths -------------------------------------------------------------------------------

def _real(path: Any, dir_fd: Optional[int] = None) -> Optional[str]:
    """The resolved absolute path, or None for a descriptor / nothing to check."""
    if path is None or isinstance(path, int):
        return None
    try:
        text = os.fsdecode(path)
    except TypeError:
        return None
    if not text:
        return None
    if not os.path.isabs(text) and dir_fd is not None:
        try:
            text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
        except OSError:
            pass
    return os.path.realpath(os.path.expanduser(text))


def _under(path: Optional[str], roots: Sequence[str]) -> Optional[str]:
    if path is None:
        return None
    for root in roots:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def live_roots(environ: Mapping[str, str] = os.environ) -> tuple[str, ...]:
    extra = [p for p in environ.get("PLX_EVAL_PROTECT", "").split(os.pathsep) if p.strip()]
    out = []
    for root in (*LIVE_ROOTS, *extra):
        real = os.path.realpath(os.path.expanduser(root))
        if real not in out:
            out.append(real)
    return tuple(out)


# --- the runtime wall --------------------------------------------------------------------

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_installed: list[tuple[str, ...]] = []
_busy = threading.local()

# event -> positions of the path arguments it writes, and of its dir_fd (or None)
_PATH_EVENTS: dict[str, tuple[tuple[int, ...], Optional[int]]] = {
    "os.remove": ((0,), 1), "os.rmdir": ((0,), 1), "os.mkdir": ((0,), 2),
    "os.rename": ((0, 1), 2), "os.link": ((1,), 3), "os.symlink": ((1,), 2),
    "os.truncate": ((0,), None), "os.chmod": ((0,), 2), "os.chown": ((0,), 3),
    "os.utime": ((0,), 3), "os.mkfifo": ((0,), 2), "os.mknod": ((0,), 3),
    "os.setxattr": ((0,), None), "os.removexattr": ((0,), None), "os.chflags": ((0,), None),
    "shutil.rmtree": ((0,), 1), "shutil.copyfile": ((1,), None),
    "shutil.copytree": ((1,), None), "shutil.move": ((0, 1), None),
    "shutil.copymode": ((1,), None), "shutil.copystat": ((1,), None),
    "shutil.chown": ((0,), None),
}


def _sqlite_target(database: Any) -> tuple[Optional[str], bool]:
    """(resolved path, opened read-only) of a ``sqlite3.connect`` database argument."""
    try:
        text = os.fsdecode(database)
    except TypeError:
        return None, False
    if not text or text == ":memory:":
        return None, True
    if text.startswith("file:"):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        ro = query.get("mode", [""])[-1] == "ro" or query.get("immutable", [""])[-1] == "1"
        path = unquote(parts.path) if parts.path else ""
        if not path or path == ":memory:" or query.get("mode", [""])[-1] == "memory":
            return None, True
        return _real(path), ro
    return _real(text), False


def _check(event: str, args: tuple, roots: Sequence[str],
           allow_generation: bool) -> None:
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(flags, int) and flags & _WRITE_FLAGS:
            write = True
        elif isinstance(mode, str):
            write = any(c in mode for c in "wax+")
        else:
            write = False
        if not write:
            return
        targets = [_real(path)]
    elif event == "sqlite3.connect":
        target, ro = _sqlite_target(args[0] if args else None)
        root = _under(target, roots)
        if root is None:
            return
        rel = os.path.relpath(target, root).split(os.sep)[0]
        if rel in RETIRED_SQLITE and not allow_generation:
            raise Refused(f"eval_guard: refused SQLite open of the retired generation {target}"
                          " (Postgres is the served store)")
        if not ro:
            raise Refused(f"eval_guard: refused read-write SQLite open under the live state "
                          f"{root}: {target} (open it file:...?mode=ro)")
        return
    elif event in _PATH_EVENTS:
        positions, fd_at = _PATH_EVENTS[event]
        dir_fd = args[fd_at] if fd_at is not None and len(args) > fd_at else None
        dir_fd = dir_fd if isinstance(dir_fd, int) else None
        targets = [_real(args[i], dir_fd) for i in positions if len(args) > i]
    else:
        return
    for target in targets:
        root = _under(target, roots)
        if root is not None:
            raise Refused(f"eval_guard: refused {event} under the live state {root}: {target}")


def install_runtime(roots: Sequence[str], *, allow_generation: bool = False) -> None:
    """The audit hook (once per process; an audit hook cannot be removed)."""
    roots = tuple(roots)
    if _installed:
        return
    _installed.append(roots)

    def hook(event: str, args: tuple) -> None:
        if getattr(_busy, "on", False):
            return
        if event not in _PATH_EVENTS and event not in ("open", "sqlite3.connect"):
            return
        _busy.on = True
        try:
            _check(event, args, roots, allow_generation)
        finally:
            _busy.on = False

    sys.addaudithook(hook)


def install_from_env(environ: Mapping[str, str] = os.environ) -> None:
    """The children's entry (``eval_guard_site/sitecustomize.py``)."""
    if environ.get(MARK) != "1":
        return
    try:
        roots = tuple(json.loads(environ.get(ROOTS_ENV) or "[]")) or live_roots(environ)
    except ValueError:
        roots = live_roots(environ)
    install_runtime(roots, allow_generation=environ.get("PLX_EVAL_ALLOW_SQLITE_GENERATION")
                    == "1")


# --- configured write paths --------------------------------------------------------------

def _flag_values(argv: Sequence[str], names: Iterable[str]) -> list[tuple[str, str]]:
    names = set(names)
    out = []
    for i, arg in enumerate(argv):
        if "=" in arg and arg.startswith("--"):
            name, value = arg.split("=", 1)
            if name in names:
                out.append((name, value))
        elif arg in names and i + 1 < len(argv):
            out.append((arg, argv[i + 1]))
    return out


def _inner_argvs(argv: Sequence[str]) -> list[list[str]]:
    """The flag lists an eval passes on to an in-process API (``--api-args "..."``)."""
    out = []
    for _, value in _flag_values(argv, ("--api-args",)):
        try:
            out.append(shlex.split(value))
        except ValueError as exc:
            raise Refused(f"eval_guard: refused unreadable --api-args ({exc})") from None
    return out


def write_paths(argv: Sequence[str], environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """(where it was configured, resolved path) for each configured write target."""
    out = []
    for name, value in _flag_values(argv, WRITE_FLAGS):
        out.append((name, _real(value) or value))
    for inner in _inner_argvs(argv):
        for name, value in _flag_values(inner, WRITE_FLAGS):
            out.append((f"--api-args {name}", _real(value) or value))
    for name, value in environ.items():
        if WRITE_ENV.match(name) and value.strip():
            for part in value.split(os.pathsep):
                if part.strip():
                    out.append((name, _real(part) or part))
    return out


def _api_args_refusal(argv: Sequence[str], roots: Sequence[str]) -> Optional[str]:
    """An in-process API needs its own app data and sessions file: the default sessions
    file is the state directory's, the default app data folder is the user's."""
    for inner in _inner_argvs(argv):
        state = _flag_values(inner, ("--state-dir",))
        if not state and "--dev" not in inner:
            continue
        given = {name for name, _ in _flag_values(inner, ("--app-data-dir", "--session-store"))}
        for need in ("--app-data-dir", "--session-store"):
            if need not in given:
                return f"--api-args runs an API without its own {need} (the default is shared)"
    return None


def _dsn_user(dsn: str) -> tuple[Optional[str], bool]:
    """(user named in a libpq DSN, whether it carries a password); nothing is printed."""
    if re.match(r"^postgres(ql)?://", dsn):
        parts = urlsplit(dsn)
        query = parse_qs(parts.query)
        user = unquote(parts.username) if parts.username else query.get("user", [None])[-1]
        return user, bool(parts.password or query.get("password"))
    pairs = dict(re.findall(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn))
    user = pairs.get("user")
    return (user.strip("'") if user else None), "password" in pairs


def dsns(argv: Sequence[str], environ: Mapping[str, str]) -> list[tuple[str, str]]:
    out = [(name, value) for name, value in environ.items() if DSN_ENV.match(name) and value]
    out += _flag_values(argv, DSN_FLAGS)
    for inner in _inner_argvs(argv):
        out += [(f"--api-args {n}", v) for n, v in _flag_values(inner, DSN_FLAGS)]
    return out


def _uses_pg(argv: Sequence[str], environ: Mapping[str, str]) -> bool:
    mode = environ.get("PLX_ONTOLOGY_PG", "").strip().lower()
    return mode not in ("", "0", "off", "no", "false") or bool(dsns(argv, environ))


def probe_pg(dsn: str) -> tuple[str, bool, str]:
    """(current_user, superuser, default_transaction_read_only) of a login with ``dsn``."""
    import psycopg  # the optional dependency the Postgres store already needs
    with psycopg.connect(dsn, connect_timeout=10, user=EVAL_ROLE) as conn:
        row = conn.execute(
            "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),"
            " current_setting('default_transaction_read_only')").fetchone()
    return str(row[0]), bool(row[1]), str(row[2])


# --- the entry -----------------------------------------------------------------------------

def check(argv: Sequence[str], environ: MutableMapping[str, str], *, entry: str,
          probe: Any = probe_pg) -> dict[str, Any]:
    """Validate and set the environment; raise ``Refused``. ``environ`` is changed in place
    (own folders, the eval role, the children's marker)."""
    roots = live_roots(environ)
    for where, path in write_paths(argv, environ):
        root = _under(path, roots)
        if root is not None:
            raise Refused(f"eval_guard: refused {where}: writes under the live state {root}")
    why = _api_args_refusal(argv, roots)
    if why:
        raise Refused(f"eval_guard: refused {why}")
    for where, dsn in dsns(argv, environ):
        user, secret = _dsn_user(dsn)
        if secret:
            raise Refused(f"eval_guard: refused {where}: a DSN carrying a password "
                          "(the eval role logs in by peer map)")
        if user not in (None, EVAL_ROLE):
            raise Refused(f"eval_guard: refused {where}: names Postgres user {user!r}, "
                          f"not the read-only {EVAL_ROLE}")

    home = Path(environ.get("PLX_EVAL_HOME") or Path(environ.get("HOME", "~")).expanduser()
                / ".cache" / "plx-eval" / re.sub(r"[^A-Za-z0-9_.-]", "_", entry))
    home_real = _real(home)
    if _under(home_real, roots):
        raise Refused(f"eval_guard: refused PLX_EVAL_HOME under the live state: {home_real}")
    home = Path(home_real or home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    environ["PLX_EVAL_HOME"] = str(home)
    environ.setdefault("XDG_STATE_HOME", str(home / "xdg-state"))
    environ.setdefault("PLX_FEEDBACK_DIR", str(home / "feedback"))
    environ.setdefault("PLX_MODEL_LEDGER", str(home / "model-calls.jsonl"))

    environ["PGUSER"] = EVAL_ROLE
    environ.pop("PGPASSWORD", None)
    environ.pop("PGPASSFILE", None)
    if _uses_pg(argv, environ):
        dsn = next((v for _, v in dsns(argv, environ)), "") or "postgresql:///plx"
        try:
            user, superuser, read_only = probe(dsn)
        except Refused:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure to prove the role refuses
            raise Refused(f"eval_guard: refused: cannot log in to Postgres as {EVAL_ROLE} "
                          f"({type(exc).__name__}); see deploy/pg/README-roles.md") from None
        if user != EVAL_ROLE or superuser or read_only != "on":
            raise Refused(f"eval_guard: refused: Postgres login is {user!r} (superuser="
                          f"{superuser}, read_only={read_only}), not the read-only {EVAL_ROLE}")

    if not (SITE_DIR / "sitecustomize.py").is_file():   # the children's wall (fail closed)
        raise Refused(f"eval_guard: refused: {SITE_DIR}/sitecustomize.py is missing, so "
                      "child processes would run unguarded")
    environ[MARK] = "1"
    environ[ROOTS_ENV] = json.dumps(list(roots))
    site = str(SITE_DIR)
    parts = [p for p in environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if site not in parts:
        environ["PYTHONPATH"] = os.pathsep.join([site, *parts])
    return {"roots": roots, "home": str(home)}


def enforce(argv: Optional[Sequence[str]] = None, *, entry: Optional[str] = None) -> None:
    """The first call of every eval entry point: check, then wall off the live state for
    this process and its children. A refusal exits 2."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if entry is None:
        main = sys.modules.get("__main__")
        spec = getattr(main, "__spec__", None)
        entry = (spec.name if spec is not None else
                 Path(getattr(main, "__file__", "eval") or "eval").stem)
    try:
        info = check(argv, os.environ, entry=entry)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    install_runtime(info["roots"], allow_generation=os.environ.get(
        "PLX_EVAL_ALLOW_SQLITE_GENERATION") == "1")
