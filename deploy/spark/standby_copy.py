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
import subprocess
import sys
import tempfile
import time

SKIP_CONTENTS = ("pg_wal", "pg_replslot", "pg_dynshmem", "pg_notify", "pg_serial",
                 "pg_snapshots", "pg_stat_tmp", "pg_subtrans")
SKIP_FILES = ("postmaster.pid", "postmaster.opts", "standby.signal", "recovery.signal",
              "backup_label", "tablespace_map")
SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"


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
    procs = []
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
        procs.append(subprocess.Popen(["bash", "-o", "pipefail", "-c", pipe]))
    codes = [p.wait() for p in procs]
    shutil.rmtree(work, ignore_errors=True)
    # GNU tar exits 1 for a file changed or removed while read: the WAL replay mends it
    failed = [i for i, c in enumerate(codes) if c not in (0, 1)]
    row = sess.one("SELECT s.lsn || ' ' || replace(encode(convert_to(s.labelfile, 'UTF8'), 'base64'), "
                   "E'\\n', '') || ' ' || replace(encode(convert_to(coalesce(s.spcmapfile, ''), 'UTF8'), "
                   "'base64'), E'\\n', '') FROM pg_backup_stop(false) s")
    sess.close()
    print(f"streams done in {time.monotonic() - started:.0f} s, exit codes {codes}", flush=True)
    if failed:
        print(f"FAILED streams {failed}: remove {a.host}:~/{a.dest} before a new copy", file=sys.stderr)
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
