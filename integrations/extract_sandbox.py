"""The operating-system boundary of an attachment extraction process (INT-015).

The mail capture reads attachment text in extraction processes
(``integrations.extract_worker``) that parse untrusted bytes with pypdf, MarkItDown,
poppler and tesseract. They already start with a scrubbed environment
(``capture_mail.extraction_env``); this module adds a kernel-enforced boundary with
Landlock (Linux 5.13+, unprivileged, stdlib ``ctypes`` only), applied by the process to
itself before it reads its first request, inherited by every tool it runs:

* files: read and execute only under the system and Python directories, the repository
  and a short list of ``/etc`` files the parsers and tools load; read and write only under
  the temporary directory and ``/dev/null``. Nothing else: not the home directory (the
  capture's credential files live there), not ``/etc/plx``, not ``/proc`` (other
  processes' environments hold credentials), not the capture stores;
* network (ABI 4+): no TCP connect and no TCP bind;
* scope (ABI 6+): no abstract UNIX socket and no signal outside the process's own domain.
* memory: an address-space limit (``RLIMIT_AS``, :data:`MEMORY_BYTES`) on the process and
  its tools.

* sockets: a seccomp filter makes ``socket()`` (and ``io_uring_setup``) fail with
  ``EACCES``: Landlock does not govern ``connect`` on a pathname UNIX socket (the
  PostgreSQL socket, where peer authentication would admit this account), and a parser
  has no use for any socket.

A kernel without Landlock, or with an ABI below :data:`MIN_ABI`, gets no
boundary: :func:`apply` reports that (``state: unavailable``) and the capture decides
(``PLX_EXTRACT_SANDBOX``: ``required`` refuses to start a process without it,
``best_effort`` (default) runs it and reports the state, ``off`` skips it).
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import resource
import sys
import tempfile
from typing import Any, Iterable

# Syscall numbers (the same on x86_64 and aarch64: the generic table).
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_RULE_NET_PORT = 2
_PR_SET_NO_NEW_PRIVS = 38

# File access rights by ABI.
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_REMOVE_DIR, _REMOVE_FILE, _MAKE_CHAR, _MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
_MAKE_REG, _MAKE_SOCK, _MAKE_FIFO, _MAKE_BLOCK, _MAKE_SYM = (1 << 8, 1 << 9, 1 << 10,
                                                            1 << 11, 1 << 12)
_REFER, _TRUNCATE, _IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
_FS_V1 = (1 << 13) - 1
_NET_BIND_TCP, _NET_CONNECT_TCP = 1 << 0, 1 << 1
_SCOPE_ABSTRACT_UNIX, _SCOPE_SIGNAL = 1 << 0, 1 << 1

MIN_ABI = 4          # TCP connect/bind rules: below it the boundary leaves the network open
MEMORY_BYTES = 6 << 30
MODES = ("required", "best_effort", "off")

_READ = _READ_FILE | _READ_DIR
_READ_EXEC = _READ | _EXECUTE
# /etc files the parsers, poppler, tesseract and the dynamic loader read.
_ETC = ("ld.so.cache", "ld.so.conf", "ld.so.conf.d", "localtime", "fonts", "mime.types",
        "nsswitch.conf", "passwd", "group", "ImageMagick-6", "ImageMagick-7", "magic",
        "magic.mime", "gai.conf", "host.conf", "alternatives")
_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/opt")
_PROC_FILES = ("/proc/cpuinfo", "/proc/meminfo", "/sys/devices/system/cpu",
               "/sys/fs/cgroup")


def _libc() -> Any:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi() -> int:
    """The kernel's Landlock ABI version; 0 when Landlock is absent or disabled."""
    if not sys.platform.startswith("linux"):
        return 0
    found = _libc().syscall(_CREATE_RULESET, None, ctypes.c_size_t(0),
                            ctypes.c_uint32(_CREATE_RULESET_VERSION))
    return int(found) if found > 0 else 0


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _fs_rights(version: int) -> int:
    rights = _FS_V1
    if version >= 2:
        rights |= _REFER
    if version >= 3:
        rights |= _TRUNCATE
    if version >= 5:
        rights |= _IOCTL_DEV
    return rights


def default_paths(repo_root: str | Path) -> tuple[list[str], list[str]]:
    """(read+execute paths, read+write paths) of an extraction process here."""
    read = [*_SYSTEM, *(f"/etc/{name}" for name in _ETC), *_PROC_FILES,
            sys.prefix, sys.base_prefix, sys.exec_prefix, str(repo_root)]
    read += [entry for entry in sys.path if entry and entry.startswith(("/usr", sys.prefix))]
    write = [tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/null", "/dev/shm"]
    return read, write


def apply(repo_root: str | Path, *, mode: str = "best_effort",
          read: Iterable[str] | None = None, write: Iterable[str] | None = None,
          memory_bytes: int | None = MEMORY_BYTES) -> dict[str, Any]:
    """Restrict this process (and what it runs) as the module docstring says. Returns
    ``{"state": "enforced" | "unavailable" | "off", "abi": n, ...}``; raises
    ``RuntimeError`` when ``mode`` is ``required`` and the boundary cannot be made."""
    if mode not in MODES:
        raise RuntimeError(f"extract sandbox mode {mode!r} unknown")
    if mode == "off":
        return {"state": "off", "abi": 0}
    if memory_bytes:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            limit = memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        except (OSError, ValueError):
            pass
    version = abi()
    if version < MIN_ABI:
        if mode == "required":
            raise RuntimeError(f"landlock ABI {version} below {MIN_ABI}")
        return {"state": "unavailable", "abi": version}
    defaults = default_paths(repo_root)
    read = defaults[0] if read is None else list(read)
    write = defaults[1] if write is None else list(write)
    libc = _libc()
    fs = _fs_rights(version)
    attr = _RulesetAttr(fs, _NET_BIND_TCP | _NET_CONNECT_TCP,
                        (_SCOPE_ABSTRACT_UNIX | _SCOPE_SIGNAL) if version >= 6 else 0)
    size = ctypes.sizeof(_RulesetAttr) if version >= 6 else 16
    ruleset = libc.syscall(_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size),
                           ctypes.c_uint32(0))
    if ruleset < 0:
        return _failed(mode, version, "create_ruleset", ctypes.get_errno())
    try:
        added = 0
        file_only = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
        for paths, rights in ((read, _READ_EXEC), (write, fs & ~_EXECUTE)):
            for path in dict.fromkeys(paths):
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue   # absent here: nothing to allow
                try:
                    allowed = rights & fs
                    if not os.path.isdir(path):
                        allowed &= file_only
                    rule = _PathBeneath(allowed, fd)
                    if libc.syscall(_ADD_RULE, ctypes.c_int(ruleset),
                                    ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(rule),
                                    ctypes.c_uint32(0)) < 0:
                        return _failed(mode, version, f"add_rule {path}", ctypes.get_errno())
                    added += 1
                finally:
                    os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return _failed(mode, version, "no_new_privs", ctypes.get_errno())
        if libc.syscall(_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) < 0:
            return _failed(mode, version, "restrict_self", ctypes.get_errno())
        refused = _no_sockets(libc)
        if refused is not None:
            return _failed(mode, version, "seccomp", refused)
    finally:
        os.close(ruleset)
    return {"state": "enforced", "abi": version, "rules": added,
            "network": "tcp_denied", "sockets": "denied", "scoped": version >= 6}


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


# (audit arch, socket, io_uring_setup) per machine.
_SECCOMP_ARCH = {"x86_64": (0xC000003E, 41, 425), "aarch64": (0xC00000B7, 198, 425)}


def _no_sockets(libc: Any) -> int | None:
    """Install the socket filter; None when installed, else the errno (or -1: an
    architecture this filter does not know)."""
    known = _SECCOMP_ARCH.get(os.uname().machine)
    if known is None:
        return -1
    arch, socket_nr, uring_nr = known
    ld_abs, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    errno_eacces, allow, kill = 0x00050000 | 13, 0x7FFF0000, 0x80000000
    program = [(ld_abs, 0, 0, 4),            # the calling architecture
               (jeq, 1, 0, arch),
               (ret, 0, 0, kill),            # another ABI (e.g. int 0x80): never served
               (ld_abs, 0, 0, 0),            # the syscall number
               (jge, 3, 0, 0x40000000),      # x32 numbers: refused
               (jeq, 2, 0, socket_nr),
               (jeq, 1, 0, uring_nr),
               (ret, 0, 0, allow),
               (ret, 0, 0, errno_eacces)]
    filters = (_SockFilter * len(program))(*[_SockFilter(*f) for f in program])
    fprog = _SockFprog(len(program), filters)
    if libc.prctl(22, 2, ctypes.byref(fprog), 0, 0) != 0:   # PR_SET_SECCOMP, FILTER
        return ctypes.get_errno()
    return None


def _failed(mode: str, version: int, step: str, errno: int) -> dict[str, Any]:
    if mode == "required":
        raise RuntimeError(f"landlock {step} failed: errno {errno}")
    return {"state": "unavailable", "abi": version, "error": f"{step}: errno {errno}"}


def mode_from(environ: Any) -> str:
    """``PLX_EXTRACT_SANDBOX`` (default ``best_effort``); an unknown value is
    ``required`` (fail closed: a typo never turns the boundary off)."""
    value = (environ.get("PLX_EXTRACT_SANDBOX") or "best_effort").strip().lower()
    return value if value in MODES else "required"
