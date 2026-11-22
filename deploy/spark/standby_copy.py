#!/usr/bin/env python3
"""Copy the box's Postgres data folder to a Spark as the base of a hot standby: a
non-exclusive base backup (``pg_backup_start`` .. ``pg_backup_stop`` held in one session),
the files split by size into ``--streams`` parallel compressed streams
(``tar | zstd | ssh | zstd -d | tar -x``), then ``backup_label``, ``tablespace_map``,
``standby.signal`` and the standby's settings written on the Spark.

    standby_copy.py --host s7d3d --dest plx-ai/pgdata [--streams 12] --go
    standby_copy.py --src DIR --psql "psql -h DIR -p PORT -d postgres" --as-user "" \
        --host s7d3d --dest plx-ai/standby-dryrun --slot "" --go     (a scratch cluster)

Without ``--go`` it only plans: the file count, bytes per stream and the commands.
The physical slot (``--slot``, default plx_s7d3d) is made before the backup starts, so the
primary keeps every WAL file the standby will need from the backup's start (bounded by the
box's ``max_slot_wal_keep_size``). Left out on the copy: ``pg_wal`` contents (streamed from
the slot), ``postmaster.pid``/``.opts``, ``pg_replslot`` contents and the folders Postgres
empties at start. Reads run as ``--as-user`` (postgres) at idle CPU and IO priority; the
ssh side runs as the caller with the Spark's ssh config. Nothing on the box is written
except the slot. A failed stream stops the backup (``pg_backup_stop``) and exits non-zero:
the Spark folder is then incomplete and must be removed before a new copy.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SKIP_CONTENTS = ("pg_wal", "pg_replslot", "pg_dynshmem", "pg_notify", "pg_serial",
                 "pg_snapshots", "pg_stat_tmp", "pg_subtrans")
SKIP_FILES = ("postmaster.pid", "postmaster.opts", "standby.signal", "recovery.signal",
              "backup_label", "tablespace_map")
SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"


def folder_made(path: str) -> bool:
    """Whether a data-folder directory is made on the Spark: not a replication slot's folder
    (its state file is never copied, and a slot folder without one stops the standby's
    start: 'could not open file pg_replslot/<slot>/state', 11-22)."""
    return not path.startswith("pg_replslot/")


def kept(path: str) -> bool:
    """Whether a data-folder file (relative path) is copied."""
    top = path.split("/", 1)[0]
    if top in SKIP_CONTENTS and "/" in path:
        return False
    return path not in SKIP_FILES and "/pgsql_tmp" not in "/" + path


def split(files: list[tuple[int, str]], streams: int) -> list[list[str]]:
    """Files (size, path) into ``streams`` lists of near-equal bytes, largest first."""
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(max(1, streams))]
    for size, path in sorted(files, reverse=True):
        i = min(range(len(bins)), key=lambda b: bins[b][0])
        bins[i] = (bins[i][0] + size, bins[i][1] + [path])
    return [paths for _, paths in bins if paths]


class Session:
    """One psql session held open: a non-exclusive backup must stop where it started."""

    def __init__(self, psql: list[str]) -> None:
        self.p = subprocess.Popen(psql + ["-X", "-At", "-v", "ON_ERROR_STOP=1"], text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def one(self, sql: str) -> str:
        assert self.p.stdin and self.p.stdout
        self.p.stdin.write(sql.rstrip(";") + ";\n\\echo __END__\n")
        self.p.stdin.flush()
        out = []
        for line in self.p.stdout:
            if line.rstrip("\n") == "__END__":
                return "\n".join(out)
            out.append(line.rstrip("\n"))
        raise RuntimeError("psql ended: " + sql[:60])

    def close(self) -> None:
        if self.p.stdin:
            self.p.stdin.close()
        self.p.wait(timeout=60)


def stream_bytes(pid: int) -> int | None:
    """Bytes the stream's compressor has read (its zstd runs as the caller, so its /proc io
    is readable without sudo); None when the stream's processes are gone."""
    try:
        kids = open(f"/proc/{pid}/task/{pid}/children").read().split()
    except OSError:
        return None
    total = 0
    for kid in kids:
        try:
            if open(f"/proc/{kid}/comm").read().strip() != "zstd":
                continue
            for line in open(f"/proc/{kid}/io"):
                if line.startswith("rchar:"):
                    total += int(line.split()[1])
        except OSError:
            continue
    return total


def watch(procs: list, stall_s: float, *, bytes_of=stream_bytes, now=time.monotonic,
          sleep=time.sleep, every: float = 30.0) -> list[int]:
    """Each stream's exit code; a stream that moves no bytes for ``stall_s`` is stopped (its
    process group: 11-20 two of 12 streams hung on a dead remote extract while the slot held
    WAL), so it fails instead of holding the copy open."""
    seen = {i: (None, now()) for i in range(len(procs))}
    while any(p.poll() is None for p in procs):
        t = now()
        for i, p in enumerate(procs):
            if p.poll() is not None:
                continue
            got = bytes_of(p.pid)
            if got is None or got != seen[i][0]:
                seen[i] = (got, t)
            elif t - seen[i][1] >= stall_s:
                print(f"stream {i}: no bytes for {stall_s:.0f} s, stopping it", file=sys.stderr,
                      flush=True)
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    p.terminate()
                seen[i] = (got, t)
        sleep(every)
    return [p.wait() for p in procs]


def launch(pipe: str) -> "subprocess.Popen":
    return subprocess.Popen(["bash", "-o", "pipefail", "-c", pipe], start_new_session=True)


def resend(pipes: list[str], failed: list[int], retries: int, stall_s: float, *,
           start=launch, wait=None) -> "list[int]":
    """The streams still failed after up to ``retries`` rounds of sending each failed one
    again whole (while the backup is open: tar -x overwrites its files, and the WAL from the
    backup's start mends what changed). 11-22: one ssh channel through the proxy hung twice
    at ~90% of a copy, and each time the whole 489 GiB copy was thrown away."""
    wait = wait or (lambda procs: watch(procs, stall_s))
    for attempt in range(retries):
        if not failed:
            break
        print(f"streams {failed}: sending again (retry {attempt + 1} of {retries})",
              file=sys.stderr, flush=True)
        codes = wait([start(pipes[i]) for i in failed])
        failed = [i for i, c in zip(failed, codes) if c not in (0, 1)]
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--dest", required=True, help="data folder on the Spark (relative to its home)")
    ap.add_argument("--src", default="/data/pg/17/main")
    ap.add_argument("--streams", type=int, default=12)
    ap.add_argument("--slot", default="plx_s7d3d", help="'' = no slot (a scratch dry run)")
    ap.add_argument("--psql", default="sudo -n -u postgres psql -d postgres")
    ap.add_argument("--as-user", default="postgres", help="'' = read the files as the caller")
    ap.add_argument("--ssh-config", default=SSH_CONFIG)
    ap.add_argument("--zstd-level", type=int, default=3)
    ap.add_argument("--stall-minutes", type=float, default=10,
                    help="stop a stream that moves no bytes this long (it then fails)")
    ap.add_argument("--stream-retries", type=int, default=2,
                    help="times a failed or stalled stream is sent again before the copy fails")
    ap.add_argument("--go", action="store_true", help="copy (without it: plan only)")
    a = ap.parse_args(argv)
    sudo = ["sudo", "-n", "-u", a.as_user] if a.as_user else []
    slow = ["nice", "-n", "19", "ionice", "-c", "3"]
    listing = subprocess.run(sudo + ["find", a.src, "-type", "f", "-printf", "%s %P\\0"],
                             capture_output=True, check=True).stdout.decode()
    files = [(int(s), p) for s, p in (e.split(" ", 1) for e in listing.split("\0") if e)]
    files = [f for f in files if kept(f[1])]
    dirs = subprocess.run(sudo + ["find", a.src, "-mindepth", "1", "-type", "d", "-printf", "%P\\n"],
                          capture_output=True, check=True, text=True).stdout.split()
    dirs = [d for d in dirs if folder_made(d)]
    parts = split(files, a.streams)
    total = sum(s for s, _ in files)
    print(f"plan: {len(files)} files, {total / 2**30:.1f} GiB, {len(parts)} streams of "
          f"~{total / max(1, len(parts)) / 2**30:.1f} GiB -> {a.host}:~/{a.dest}", flush=True)
    if not a.go:
        return 0
    ssh = ["ssh", "-F", a.ssh_config, "-o", "BatchMode=yes", a.host]
    dest = shlex.quote(a.dest)
    subprocess.run(ssh + [f"test ! -e {dest}/PG_VERSION && mkdir -p -m 700 {dest} && cd {dest} && "
                          f"xargs -0 mkdir -p -m 700"], input="\0".join(dirs).encode(), check=True)
    sess = Session(shlex.split(a.psql))
    if a.slot:
        sess.one(f"SELECT pg_create_physical_replication_slot('{a.slot}', true) "
                 f"WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '{a.slot}')")
    print("backup start lsn", sess.one("SELECT pg_backup_start('plx-standby', false)"), flush=True)
    work = tempfile.mkdtemp(prefix="standby-copy-")  # tmp-lint: standalone box script; the sudo'd tar reads its lists, removed once the streams end
    os.chmod(work, 0o755)
    procs, pipes = [], []
    started = time.monotonic()
    for i, paths in enumerate(parts):
        lst = os.path.join(work, f"s{i}.list")
        with open(lst, "w") as f:
            f.write("\0".join(paths) + "\0")
        os.chmod(lst, 0o644)
        tar = sudo + slow + ["tar", "-C", a.src, "--null", "-T", lst, "--ignore-failed-read",
                             "--warning=no-file-changed", "--warning=no-file-removed", "-cf", "-"]
        pipe = (" ".join(map(shlex.quote, tar)) + f" | zstd -q -{a.zstd_level} -T2 | "
                + " ".join(map(shlex.quote, ssh)) + " " + shlex.quote(f"zstd -dq | tar -C {dest} -xf -"))
        pipes.append(pipe)
        procs.append(subprocess.Popen(["bash", "-o", "pipefail", "-c", pipe],
                                      start_new_session=True))
    codes = watch(procs, a.stall_minutes * 60)
    # GNU tar exits 1 for a file changed or removed while read: the WAL replay mends it
    failed = [i for i, c in enumerate(codes) if c not in (0, 1)]
    failed = resend(pipes, failed, a.stream_retries, a.stall_minutes * 60)
    shutil.rmtree(work, ignore_errors=True)
    row = sess.one("SELECT s.lsn || ' ' || replace(encode(convert_to(s.labelfile, 'UTF8'), 'base64'), "
                   "E'\\n', '') || ' ' || replace(encode(convert_to(coalesce(s.spcmapfile, ''), 'UTF8'), "
                   "'base64'), E'\\n', '') FROM pg_backup_stop(false) s")
    sess.close()
    print(f"streams done in {time.monotonic() - started:.0f} s, exit codes {codes}", flush=True)
    if failed:
        print(f"FAILED streams {failed}: remove {a.host}:~/{a.dest} before a new copy", file=sys.stderr)
        if a.slot:   # a failed copy is never resumed: its slot would only hold WAL
            drop = Session(shlex.split(a.psql))
            drop.one(f"SELECT count(pg_drop_replication_slot(slot_name)) FROM pg_replication_slots "
                     f"WHERE slot_name = '{a.slot}' AND NOT active")
            drop.close()
        return 1
    lsn, label, spcmap = (row.split(" ") + ["", ""])[:3]
    write = (f"cd {dest} && base64 -d > backup_label <<< {shlex.quote(label)} && "
             + (f"base64 -d > tablespace_map <<< {shlex.quote(spcmap)} && " if spcmap else "")
             + "touch standby.signal")
    subprocess.run(ssh + ["bash", "-c", shlex.quote(write)], check=True)
    print(f"backup stop lsn {lsn}; backup_label + standby.signal written; next: the standby's "
          "settings (standby_install.sh), its tunnel, then start plx-spark-standby", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
