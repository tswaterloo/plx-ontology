"""Copy the read side of the box's state directory to a Spark, for runs started there by
``deploy/spark/spark_run.sh`` (their ``/data/plx-state`` is this copy).

usage: python3 deploy/spark/spark_state_sync.py --host s7d3d [--jobs 4] [--blobs] [--only REL]...
       [--state /data/plx-state] [--dest plx-state-copy] [--dry-run]

What goes: the stores an eval, bench or private API reads (``ALLOW``: the capture stores,
case runtime, projection readings, autopilot/learning/answer bank, small JSON files). What never
goes: sessions and tickets, the held-out set, keys and tunnels, alarms, job queues, other modules'
work, transient delta work files (``DENY``), and the blob folders unless ``--blobs`` (attachment
and file bodies: ~120 GB that does not compress).

How, per file, all read-only on the box:
* a SQLite store in WAL mode: ``sqlite3_rsync`` (SQLite 3.54) over ssh with compression. It
  reads the store inside one read transaction (a consistent snapshot; writers carry on, a
  checkpoint only waits to truncate the WAL) and sends only the pages that differ, so a re-run
  moves what changed since the last one. The copy is updated in a transaction: a run reading it
  on the Spark sees the old or the new snapshot, never a mix.
* a SQLite store in rollback mode: a local online backup (its read lock blocks writers while it
  copies, so only files up to ``ROLLBACK_MAX`` bytes; bigger ones are reported ``skipped``),
  then rsync.
* any other file: rsync (temp name, then rename).

Prints one JSON line per file and a summary line; exit 1 when any file failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATE = "/data/plx-state"
DEST = "plx-state-copy"                    # under the Spark login's home
SSH_CONFIG = os.environ.get("PLX_SPARK_SSH_CONFIG", "/data/plx-state/ts-petra/ssh_config")
RSYNC_BIN = os.environ.get("PLX_SQLITE3_RSYNC", "/usr/local/lib/plx-ops/bin/sqlite3_rsync")
REMOTE_RSYNC_BIN = "plx-ai/bin/sqlite3_rsync"   # under the Spark login's home
ROLLBACK_MAX = 256 * 1024 * 1024
SEED_MIN = 512 * 1024 * 1024        # a store this big with no copy yet is streamed first

# top-level names under the state directory that are copied (files or whole folders)
ALLOW = (
    "mail-capture", "sharepoint-capture", "sharepoint-list-capture", "teams-capture",
    "meetings-capture", "mc-capture", "bc-capture", "org-capture", "attach-fetch",
    "case-runtime", "case-originals", "projection", "autopilot", "learning", "answer-bank",
    "doc-style", "labour", "reference", "work-graph", "host",
    "session-replay.sqlite", "console-journal.sqlite", "session-state-label.json",
    "reviews.sqlite", "ontology.sqlite.watchlists.sqlite", "team-activity.json",
    "system-activity.json",
)
# never copied, wherever it sits (matched against the path relative to the state directory)
DENY = re.compile(
    r"(^|/)("
    r"app-sessions[^/]*|console-ticket-audit[^/]*|known-readers[^/]*|[^/]*\.env|"
    r"[^/]*(secret|token|credential|passw)[^/]*|[^/]*\.(pem|key)|id_(rsa|ed25519|ecdsa)[^/]*|"
    r"logs|page-cache|work-[0-9T]+|delta-[0-9T]+\.sqlite[^/]*|[^/]*\.new|[^/]*\.tmp|"
    r"[^/]*\.lock|[^/]*-(wal|shm|journal)|\.[^/]*\.vacuum\.tmp"
    r")(/|$)", re.I)
BLOB_DIRS = re.compile(r"(^|/)(blobs|blobs\.nvme)(/|$)")
SQLITE_MAGIC = b"SQLite format 3\x00"


def ssh_base(config: str = SSH_CONFIG) -> list[str]:
    return ["ssh", "-F", config, "-o", "ServerAliveInterval=30", "-o", "BatchMode=yes"]


def kind_of(path: Path) -> str:
    """``wal`` / ``rollback`` for a SQLite file (by its header), ``file`` otherwise."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return "file"
    if not head.startswith(SQLITE_MAGIC):
        return "file"
    return "wal" if len(head) > 18 and head[18] == 2 else "rollback"


def _walk(state: Path, top: Path, blobs: bool):
    """The files under ``top`` (or ``top`` itself), never descending into a denied or (without
    ``blobs``) blob folder: those hold millions of files."""
    if top.is_file():
        yield top
        return
    for root, dirs, files in os.walk(top):
        base = Path(root)
        dirs[:] = sorted(d for d in dirs if not DENY.search((base / d).relative_to(state).as_posix())
                         and (blobs or not BLOB_DIRS.search((base / d).relative_to(state).as_posix())))
        for f in sorted(files):
            yield base / f


def plan(state: Path, *, blobs: bool = False, only: tuple[str, ...] = ()) -> list[dict]:
    """Every file to copy: ``{"rel", "size", "kind"}``, biggest first. Only ``ALLOW`` names
    (narrowed by ``only``), never a ``DENY`` path, blob folders only with ``blobs``."""
    out = []
    roots = [n for n in ALLOW if not only or any(n == o or o.startswith(n + "/") for o in only)]
    for name in roots:
        top = state / name
        for p in _walk(state, top, blobs):
            rel = p.relative_to(state).as_posix()
            if only and not any(rel == o or rel.startswith(o.rstrip("/") + "/") for o in only):
                continue
            if DENY.search(rel) or (not blobs and BLOB_DIRS.search(rel)) or p.is_symlink():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append({"rel": rel, "size": size, "kind": kind_of(p)})
    out.sort(key=lambda r: -r["size"])
    return out


def _run(cmd: list[str], *, timeout: float) -> tuple[int, str]:
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return got.returncode, (got.stderr or got.stdout or "").strip()[-400:]


def seed_stream(state: Path, rel: str, host: str, dest: str) -> dict:
    """A cold copy of a big store: its main file as it is now, zstd over one ssh stream (the
    page-by-page sync is bound by the link's round trips, ~3 MB/s a store; this moves the
    compressed bytes at the link's rate). The copy may be torn (no lock is taken: writers never
    wait); the ``sqlite3_rsync`` pass after it compares every page and makes it an exact
    snapshot."""
    remote = shlex.quote(f"{dest}/{rel}")
    cmd = (f"set -o pipefail; zstd -q -T4 -3 -c < {shlex.quote(str(state / rel))} | "
           + shlex.join(ssh_base() + [host])
           + " " + shlex.quote(f"umask 077; zstd -q -d -c > {remote}.seed && mv -f {remote}.seed {remote}"
                               f" && rm -f {remote}-journal {remote}-wal {remote}-shm"))
    try:
        got = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=8 * 3600)
    except subprocess.TimeoutExpired:
        return {"state": "failed", "error": "seed timeout"}
    return {"state": "ok" if got.returncode == 0 else "failed",
            **({} if got.returncode == 0 else {"error": (got.stderr or "")[-400:]})}


def remote_sizes(host: str, dest: str, rels: list[str]) -> dict[str, int]:
    """The size of each copy on the Spark now (absent: not in the result)."""
    if not rels:
        return {}
    script = "cd " + shlex.quote(dest) + " && for f in " + shlex.join(rels) + \
        "; do [ -f \"$f\" ] && printf '%s\\t%s\\n' \"$(stat -c %s \"$f\")\" \"$f\"; done; true"
    got = subprocess.run(ssh_base() + [host, script], capture_output=True, text=True, timeout=300)
    out = {}
    for line in got.stdout.splitlines():
        size, _, rel = line.partition("\t")
        if size.isdigit():
            out[rel] = int(size)
    return out


def sync_wal(state: Path, rel: str, host: str, dest: str, wrapper: str) -> dict:
    code, msg = _run([RSYNC_BIN, str(state / rel), f"{host}:{dest}/{rel}", "--exe", REMOTE_RSYNC_BIN,
                      "--ssh", wrapper, "--wal-only", "-v"], timeout=6 * 3600)
    sent = re.search(r"sent ([\d,]+) bytes, received ([\d,]+) bytes", msg)
    return {"state": "ok" if code == 0 else "failed", "code": code,
            "sent": int(sent.group(1).replace(",", "")) if sent else None,
            **({} if code == 0 else {"error": msg})}


def sync_rollback(state: Path, rel: str, host: str, dest: str, size: int, tmpdir: str) -> dict:
    if size > ROLLBACK_MAX:
        return {"state": "skipped", "why": "rollback_store_too_big_to_lock"}
    local = Path(tmpdir) / rel.replace("/", "__")
    try:
        src = sqlite3.connect(f"file:{state / rel}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(local)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
    except sqlite3.Error as exc:
        local.unlink(missing_ok=True)
        return {"state": "failed", "error": f"backup: {exc}"}
    try:
        return sync_file_from(local, rel, host, dest)
    finally:
        local.unlink(missing_ok=True)


def sync_file_from(local: Path, rel: str, host: str, dest: str) -> dict:
    code, msg = _run(["rsync", "-t", "--compress", "-e", shlex.join(ssh_base()), str(local),
                      f"{host}:{dest}/{rel}"], timeout=6 * 3600)
    return {"state": "ok" if code == 0 else "failed", **({} if code == 0 else {"error": msg})}


def sync_files(state: Path, rels: list[str], host: str, dest: str) -> dict:
    """The plain files in one rsync (relative paths kept)."""
    if not rels:
        return {"state": "ok", "files": 0}
    with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
        f.write("\n".join(rels) + "\n")
        lst = f.name
    try:
        code, msg = _run(["rsync", "-t", "--compress", "--files-from", lst, "-e",
                          shlex.join(ssh_base()), f"{state}/", f"{host}:{dest}/"], timeout=6 * 3600)
    finally:
        os.unlink(lst)
    return {"state": "ok" if code == 0 else "failed", "files": len(rels),
            **({} if code == 0 else {"error": msg})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, choices=("s7d3d", "sb4ec"))
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--blobs", action="store_true")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a.dest):
        ap.error("--dest is a folder name under the Spark login's home")
    state = Path(a.state)
    items = plan(state, blobs=a.blobs, only=tuple(a.only))
    if a.dry_run:
        for r in items:
            print(json.dumps(r))
        print(json.dumps({"summary": True, "files": len(items),
                          "bytes": sum(r["size"] for r in items)}))
        return 0
    if not os.access(RSYNC_BIN, os.X_OK):
        print(f"spark_state_sync: no sqlite3_rsync at {RSYNC_BIN} (deploy/spark/build_sqlite3_rsync.sh)",
              file=sys.stderr)
        return 2
    dirs = sorted({str(Path(a.dest, r["rel"]).parent) for r in items})
    code, msg = _run(ssh_base() + [a.host, "umask 077; mkdir -p -- " + shlex.join(dirs)], timeout=120)
    if code:
        print(f"spark_state_sync: cannot reach {a.host}: {msg}", file=sys.stderr)
        return 2
    wrapper_dir = tempfile.mkdtemp(prefix="plx-spark-sync-")  # tmp-lint: standalone sync script, removed in its own finally
    wrapper = os.path.join(wrapper_dir, "ssh")
    with open(wrapper, "w") as f:   # sqlite3_rsync runs --ssh as one program name
        f.write("#!/bin/sh\nexec " + shlex.join(ssh_base() + ["-C"]) + ' "$@"\n')
    os.chmod(wrapper, 0o700)
    started = time.time()
    failed = 0

    have = remote_sizes(a.host, a.dest, [r["rel"] for r in items if r["kind"] == "wal"
                                         and r["size"] >= SEED_MIN])

    def one(r: dict) -> dict:
        t = time.time()
        seeded = None
        if r["kind"] == "wal" and r["size"] >= SEED_MIN and have.get(r["rel"], 0) < r["size"] // 2:
            seeded = seed_stream(state, r["rel"], a.host, a.dest)
            seeded["s"] = round(time.time() - t, 1)
        if seeded and seeded["state"] != "ok":
            res = seeded
        elif r["kind"] == "wal":
            res = sync_wal(state, r["rel"], a.host, a.dest, wrapper)
            if seeded:
                res["seed"] = seeded
        else:
            res = sync_rollback(state, r["rel"], a.host, a.dest, r["size"], wrapper_dir)
        return {**r, **res, "s": round(time.time() - t, 1)}

    try:
        stores = [r for r in items if r["kind"] != "file"]
        with ThreadPoolExecutor(max(1, a.jobs)) as pool:
            files_job = pool.submit(sync_files, state, [r["rel"] for r in items if r["kind"] == "file"],
                                    a.host, a.dest)
            for res in pool.map(one, stores):
                failed += res["state"] == "failed"
                print(json.dumps(res), flush=True)
            fr = files_job.result()
            failed += fr["state"] == "failed"
            print(json.dumps({"rel": "<files>", **fr}), flush=True)
    finally:
        for p in Path(wrapper_dir).iterdir():
            p.unlink(missing_ok=True)
        os.rmdir(wrapper_dir)
    print(json.dumps({"summary": True, "host": a.host, "files": len(items), "failed": failed,
                      "bytes": sum(r["size"] for r in items), "s": round(time.time() - started, 1)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
