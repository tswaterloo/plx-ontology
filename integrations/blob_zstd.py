"""zstd-compressed originals of the mail capture's blob store (DISK-CLEAN-1114).

An attachment original is stored as its bytes, ``<sha256>`` under ``blobs/sha256/<aa>/``.
This module adds a fourth stored form of those bytes, ``<sha256>.zst``: one whole zstd
frame (content size in its header) of exactly the bytes, written by :func:`compress_store`
for an original that is cold (attachments are content-addressed and never rewritten), and
read through :mod:`integrations.blob_pack` (``read``/``open_blob``/``head``/``size``/
``exists``/``unlink`` of the bytes form fall back to it), so every reader of an original's
bytes reads it unchanged, re-hashed as before. A purge removes both names (``unlink`` of the
bytes form: the bytes file first, then this one).

Stdlib only: the system ``libzstd`` through ``ctypes``, else the ``zstd`` command. Without
either a ``.zst`` form reads as unreadable (``OSError``), never as other bytes.

What is compressed: only the mail store's bytes-form files (``<sha256>``, no suffix) of at
least ``--min-bytes`` that shrink by at least ``--min-saving``. Bodies and texts (``.z``,
some packed), ``.dropped`` readings and the SharePoint store (it reads its files by path)
are never touched. ``expand`` writes every ``.zst`` back as its bytes file (read back
equal) and removes it: the undo.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, BinaryIO, Optional, Sequence

ZSTD_SUFFIX = ".zst"
MAX_RAW_BYTES = 1 << 30          # an original inflates to at most this (else unreadable)
DEFAULT_LEVEL = 10
DEFAULT_MIN_BYTES = 64 * 1024    # smaller files gain little after 4 KiB block rounding
DEFAULT_MIN_SAVING = 0.10
_RAW_NAME = re.compile(r"[0-9a-f]{64}")
_UNKNOWN = 2 ** 64 - 1           # ZSTD_CONTENTSIZE_UNKNOWN
_ERROR = 2 ** 64 - 2             # ZSTD_CONTENTSIZE_ERROR
_CHUNK = 128 * 1024


class _InBuffer(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class _OutBuffer(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


@functools.lru_cache(maxsize=None)
def _lib() -> Optional[ctypes.CDLL]:
    names = [ctypes.util.find_library("zstd"), "libzstd.so.1", "libzstd.1.dylib", "libzstd.dylib"]
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        size_t, vp = ctypes.c_size_t, ctypes.c_void_p
        lib.ZSTD_isError.argtypes, lib.ZSTD_isError.restype = [size_t], ctypes.c_uint
        lib.ZSTD_getFrameContentSize.argtypes = [ctypes.c_char_p, size_t]
        lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        lib.ZSTD_findFrameCompressedSize.argtypes = [ctypes.c_char_p, size_t]
        lib.ZSTD_findFrameCompressedSize.restype = size_t
        lib.ZSTD_decompress.argtypes = [vp, size_t, ctypes.c_char_p, size_t]
        lib.ZSTD_decompress.restype = size_t
        lib.ZSTD_compressBound.argtypes, lib.ZSTD_compressBound.restype = [size_t], size_t
        lib.ZSTD_compress.argtypes = [vp, size_t, ctypes.c_char_p, size_t, ctypes.c_int]
        lib.ZSTD_compress.restype = size_t
        lib.ZSTD_createDStream.argtypes, lib.ZSTD_createDStream.restype = [], vp
        lib.ZSTD_freeDStream.argtypes, lib.ZSTD_freeDStream.restype = [vp], size_t
        lib.ZSTD_initDStream.argtypes, lib.ZSTD_initDStream.restype = [vp], size_t
        lib.ZSTD_decompressStream.argtypes = [vp, ctypes.POINTER(_OutBuffer),
                                              ctypes.POINTER(_InBuffer)]
        lib.ZSTD_decompressStream.restype = size_t
        return lib
    return None


def _cli(args: Sequence[str], data: bytes) -> bytes:
    try:
        done = subprocess.run(["zstd", "-q", *args], input=data, capture_output=True,
                              check=False, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"zstd_unavailable:{type(exc).__name__}") from exc
    if done.returncode != 0:
        raise ValueError("zstd_frame_invalid")
    return done.stdout


def content_size(frame: bytes) -> int:
    """The bytes' size a frame's header states; ``ValueError`` when it states none."""
    if len(frame) < 4 or frame[:4] != b"\x28\xb5\x2f\xfd":
        raise ValueError("zstd_frame_invalid")
    lib = _lib()
    if lib is None:   # the header holds it, but parsing it by hand is not worth it
        return len(decompress(frame))
    found = lib.ZSTD_getFrameContentSize(frame, len(frame))
    if found in (_UNKNOWN, _ERROR):
        raise ValueError("zstd_size_unknown")
    return int(found)


def decompress(frame: bytes, limit: int = MAX_RAW_BYTES) -> bytes:
    """The bytes of one whole zstd frame (nothing after it, content size stated, at most
    ``limit``); ``ValueError`` otherwise, ``OSError`` when no zstd is available."""
    lib = _lib()
    if lib is None:
        out = _cli(["-d", "-c"], frame)
        if len(out) > limit:
            raise ValueError("zstd_too_big")
        return out
    if lib.ZSTD_findFrameCompressedSize(frame, len(frame)) != len(frame):
        raise ValueError("zstd_frame_invalid")   # an error code, or bytes after the frame
    size = content_size(frame)
    if size > limit:
        raise ValueError("zstd_too_big")
    buffer = ctypes.create_string_buffer(max(size, 1))
    got = lib.ZSTD_decompress(buffer, size, frame, len(frame))
    if lib.ZSTD_isError(got) or got != size:
        raise ValueError("zstd_frame_invalid")
    return buffer.raw[:size]


def head(handle: BinaryIO, size: int) -> bytes:
    """The first ``size`` bytes a frame read from ``handle`` inflates to (unverified, as
    any head), reading only as much of the frame as that takes."""
    lib = _lib()
    if lib is None:
        return decompress(handle.read())[:size]
    stream = lib.ZSTD_createDStream()
    if not stream:
        raise MemoryError("zstd_stream")
    try:
        if lib.ZSTD_isError(lib.ZSTD_initDStream(stream)):
            raise ValueError("zstd_frame_invalid")
        out_mem = ctypes.create_string_buffer(max(size, 1))
        out = _OutBuffer(ctypes.cast(out_mem, ctypes.c_void_p), size, 0)
        while out.pos < size:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            in_mem = ctypes.create_string_buffer(chunk, len(chunk))
            inb = _InBuffer(ctypes.cast(in_mem, ctypes.c_void_p), len(chunk), 0)
            while inb.pos < inb.size and out.pos < size:
                ret = lib.ZSTD_decompressStream(stream, ctypes.byref(out), ctypes.byref(inb))
                if lib.ZSTD_isError(ret):
                    raise ValueError("zstd_frame_invalid")
                if ret == 0:          # the frame ended
                    return out_mem.raw[:out.pos]
        return out_mem.raw[:out.pos]
    finally:
        lib.ZSTD_freeDStream(stream)


def compress(data: bytes, level: int = DEFAULT_LEVEL) -> bytes:
    """One zstd frame of ``data`` (content size in its header)."""
    lib = _lib()
    if lib is None:
        return _cli([f"-{level}", "-c"], data)
    bound = lib.ZSTD_compressBound(len(data))
    buffer = ctypes.create_string_buffer(bound)
    got = lib.ZSTD_compress(buffer, bound, data, len(data), level)
    if lib.ZSTD_isError(got):
        raise ValueError("zstd_compress_failed")
    return buffer.raw[:got]


# --------------------------------------------------------------------------- the store pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(target: Path, data: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_dir(target.parent)


def _shard_dirs(blobs: Path, shards: Optional[Sequence[str]]) -> list[Path]:
    names = sorted(shards) if shards else sorted(
        p.name for p in blobs.iterdir() if p.is_dir() and re.fullmatch(r"[0-9a-f]{2}", p.name))
    return [blobs / name for name in names if re.fullmatch(r"[0-9a-f]{2}", name)]


def compress_one(raw: Path, *, level: int, min_bytes: int, min_saving: float,
                 write: bool) -> tuple[str, int]:
    """(outcome, bytes saved) of one bytes-form file. The ``.zst`` is written (fsynced) and
    read back equal before the bytes file goes; a bytes file deleted meanwhile (a purge or
    a drop) takes the new ``.zst`` with it, so a deleted original never comes back."""
    digest = raw.name
    target = raw.with_name(digest + ZSTD_SUFFIX)
    try:
        size = raw.stat().st_size
    except FileNotFoundError:
        return "gone", 0
    if size < min_bytes:
        return "small", 0
    if target.exists():   # both held (written again by the capture): keep the frame
        try:
            same = hashlib.sha256(decompress(target.read_bytes())).hexdigest() == digest
        except (OSError, ValueError):
            same = False
        if not same:
            return "zst_differs", 0
        if write:
            raw.unlink(missing_ok=True)
        return "already", size
    try:
        content = raw.read_bytes()
    except FileNotFoundError:
        return "gone", 0
    if hashlib.sha256(content).hexdigest() != digest:
        return "altered", 0     # never compressed: a reader refuses it either way
    frame = compress(content, level)
    if len(frame) > size * (1 - min_saving):
        return "incompressible", 0
    if decompress(frame) != content:
        return "roundtrip_differs", 0
    if not write:
        return "compressed", size - len(frame)
    _write(target, frame)
    if decompress(target.read_bytes()) != content:
        target.unlink(missing_ok=True)
        return "readback_differs", 0
    try:
        raw.unlink()
    except FileNotFoundError:
        target.unlink(missing_ok=True)   # deleted while compressing: so is its new form
        return "gone", 0
    return "compressed", size - len(frame)


def compress_store(blobs: str | Path, *, write: bool = False,
                   shards: Optional[Sequence[str]] = None, level: int = DEFAULT_LEVEL,
                   min_bytes: int = DEFAULT_MIN_BYTES, min_saving: float = DEFAULT_MIN_SAVING,
                   limit: Optional[int] = None, deadline: Optional[float] = None
                   ) -> dict[str, Any]:
    out: dict[str, Any] = {"write": write, "files": 0, "bytes_saved": 0, "stopped": None}
    for shard in _shard_dirs(Path(blobs), shards):
        if deadline is not None and time.monotonic() > deadline:
            out["stopped"] = f"deadline_before:{shard.name}"
            break
        for raw in sorted(shard.iterdir()):
            if _RAW_NAME.fullmatch(raw.name) is None:
                continue
            if limit is not None and out["files"] >= limit:
                out["stopped"] = "limit"
                return out
            outcome, saved = compress_one(raw, level=level, min_bytes=min_bytes,
                                          min_saving=min_saving, write=write)
            out[outcome] = out.get(outcome, 0) + 1
            out["bytes_saved"] += saved
            if outcome == "compressed":
                out["files"] += 1
    return out


def verify_store(blobs: str | Path, shards: Optional[Sequence[str]] = None) -> dict[str, int]:
    """Every ``.zst`` inflates to its digest's bytes (``bad`` otherwise); ``also_raw``:
    held both ways (harmless: the bytes file is read first)."""
    out = {"zst": 0, "bad": 0, "also_raw": 0}
    for shard in _shard_dirs(Path(blobs), shards):
        for path in shard.iterdir():
            if not path.name.endswith(ZSTD_SUFFIX) or path.name.startswith("."):
                continue
            digest = path.name[:-len(ZSTD_SUFFIX)]
            out["zst"] += 1
            try:
                ok = hashlib.sha256(decompress(path.read_bytes())).hexdigest() == digest
            except (OSError, ValueError):
                ok = False
            out["bad"] += not ok
            out["also_raw"] += path.with_name(digest).exists()
    return out


def expand_store(blobs: str | Path, shards: Optional[Sequence[str]] = None) -> dict[str, int]:
    """The undo: each ``.zst`` written back as its bytes file (fsynced, read back equal),
    then removed. A frame that does not inflate to its digest stays as it is (``bad``)."""
    out = {"expanded": 0, "bad": 0}
    for shard in _shard_dirs(Path(blobs), shards):
        for path in sorted(shard.iterdir()):
            if not path.name.endswith(ZSTD_SUFFIX) or path.name.startswith("."):
                continue
            digest = path.name[:-len(ZSTD_SUFFIX)]
            try:
                content = decompress(path.read_bytes())
            except (OSError, ValueError):
                out["bad"] += 1
                continue
            if hashlib.sha256(content).hexdigest() != digest:
                out["bad"] += 1
                continue
            raw = path.with_name(digest)
            if not raw.exists():
                _write(raw, content)
            if hashlib.sha256(raw.read_bytes()).hexdigest() != digest:
                out["bad"] += 1
                continue
            path.unlink()
            out["expanded"] += 1
    return out


def stale_trees(proc: Path = Path("/proc")) -> dict[str, int]:
    """Processes of this user running from a Workbench tree (working directory with an
    ``integrations/captured_text.py``) that has no ``integrations/blob_zstd.py``: code that
    would read a compressed original as missing. Broader than ``blob_pack.stale_readers``
    (which looks only at holders of ``capture.db``): a migration's step workers read the
    originals without holding it. ``possible``: this user's processes that cannot be
    looked at, other than non-Python helpers (``blob_pack.unseen_reader``)."""
    from integrations.blob_pack import unseen_reader
    out = {"stale": 0, "current": 0, "possible": 0}
    me = os.getuid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != me:
                continue
            tree = Path(os.readlink(entry / "cwd"))
        except PermissionError:
            out["possible"] += unseen_reader(entry)
            continue
        except OSError:
            continue
        if not (tree / "integrations" / "captured_text.py").is_file():
            continue
        out["current" if (tree / "integrations" / "blob_zstd.py").is_file() else "stale"] += 1
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("compress", "verify", "expand"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--blobs", required=True, type=Path,
                         help="the mail capture's blobs/sha256 directory")
        cmd.add_argument("--shards", nargs="*", help="two-hex-digit shard names")
        if name == "compress":
            cmd.add_argument("--write", action="store_true",
                             help="replace the files (default: count only)")
            cmd.add_argument("--level", type=int, default=DEFAULT_LEVEL)
            cmd.add_argument("--min-bytes", type=int, default=DEFAULT_MIN_BYTES)
            cmd.add_argument("--min-saving", type=float, default=DEFAULT_MIN_SAVING)
            cmd.add_argument("--limit", type=int)
            cmd.add_argument("--minutes", type=float, help="start no shard after this")
    args = parser.parse_args(argv)
    if not args.blobs.is_dir() or args.blobs.name != "sha256" \
            or args.blobs.parent.parent.name != "mail-capture":
        print("--blobs must be the mail capture's blobs/sha256 directory", file=sys.stderr)
        return 2
    if args.command == "compress":
        if args.write:
            readers = stale_trees()
            if readers["stale"] or readers["possible"]:
                print(json.dumps({"refused": "readers_without_zstd", **readers}),
                      file=sys.stderr)
                return 3
        deadline = None if args.minutes is None else time.monotonic() + args.minutes * 60
        result: dict[str, Any] = compress_store(
            args.blobs, write=args.write, shards=args.shards, level=args.level,
            min_bytes=args.min_bytes, min_saving=args.min_saving, limit=args.limit,
            deadline=deadline)
    elif args.command == "verify":
        result = verify_store(args.blobs, args.shards)
    else:
        result = expand_store(args.blobs, args.shards)
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("bad") or any(
        result.get(k) for k in ("zst_differs", "readback_differs", "roundtrip_differs")) else 0


if __name__ == "__main__":
    sys.exit(main())
