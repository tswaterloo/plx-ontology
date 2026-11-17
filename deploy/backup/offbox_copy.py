#!/usr/bin/env python3
"""Off-box copy of the backups to a DGX Spark over the tailnet ssh (owner, 15 Nov 2026: the
off-box copy goes to a Spark; there is no S3).

Why: /data (Postgres, capture, /data/backups) sits on one instance-store disk that is lost on
an instance stop; the capture backup on the root disk survives a stop but not the instance.

What is copied, each run: the newest healthy nightly set of ``/data/backups/daily`` (every
file the set holds, each checked against its manifest digest) and the capture backup of
``/var/backups/plx-capture`` (``manifest.json``, every piece it lists, checked against its
digest, and ``lists/``).

Encryption at rest: every file is encrypted on this box with gpg (symmetric AES256, no
compression: the pieces are compressed already) under a 256-bit random key kept only on
this box (``PLX_OFFBOX_KEY``, default ``~/.config/plx/offbox.key`` on the root disk, mode
0600, refused when anyone else may read it). The Sparks are shared machines: what lands
there is ciphertext only, under a directory owned by our user with mode 0700, files 0600.
The key never leaves the box; ``init-key`` makes it (it is never printed).

Layout on the Spark (``~/<PLX_OFFBOX_REMOTE_DIR>``, default ``plx-offbox``):
``objects/<name>.gpg`` content-addressed (``name`` = HMAC-SHA256 of the plaintext digest
under the key, so the names reveal nothing and an unchanged piece is sent once);
``copies/<stamp>.json.gpg`` one encrypted record per run (every file, its plaintext digest,
its object and the ciphertext digest); ``incoming/`` the rsync landing area. Objects are
only moved from ``incoming`` into ``objects`` after a remote ``sha256sum`` match.

Transfer: encrypted files are staged in batches under ``PLX_OFFBOX_STAGE`` and sent with
``rsync --checksum``; then the Spark computes the sha256 of each and only a match counts.
Priority order: the Postgres set, the capture manifest and lists, the capture databases,
the loose files, the blob shards (largest first within each group, so the longest single
upload starts early). A newer healthy Postgres set that appears during a run (the nightly
set lands while the first seed is still going) is put at the head of the queue, ahead of
the capture pieces not yet started, and the record names it instead of the older set.
``PLX_OFFBOX_MAX_SECONDS`` bounds a run; the next run resumes (objects already on the Spark
are not sent again).

Speed (measured 15 Nov 2026, box -> s7d3d -> sb4ec over the userspace tailnet, direct path
20 ms): one TCP stream carries ~0.7 MB/s whatever the cipher (a per-flow limit; the CPU is
idle), eight separate streams ~5.4 MB/s, sixteen dropped connections at the jump host's
sshd and stalled. So batches go out on ``PLX_OFFBOX_STREAMS`` (default 8, at most 12)
separate ssh connections (never the shared control connection, which would carry them all
in one flow), started ``PLX_OFFBOX_STAGGER`` seconds apart; a dropped rsync is retried
(``--partial`` keeps what arrived). gpg runs with compression off: the capture pieces are
zstd already and the Postgres dump is compressed.

Green (exit 0) only when: the sources are whole (a healthy Postgres set; a capture manifest
marked complete), every file is on the Spark and every object of the record matched its
sha256 on the Spark in this run, and the record itself was verified. Otherwise exit 3
(incomplete: what is missing is listed) or 1 (failed), so the unit fails and the plx-
OnFailure alarm fires; never a green line.

Spark choice: ``PLX_OFFBOX_HOSTS`` (default sb4ec only). The data host and the key-escrow
host are never the same machine: a host also listed in ``PLX_OFFBOX_ESCROW_HOSTS`` (default
s7d3d) is refused before any connection, and a configured host that holds
``~/<PLX_OFFBOX_ESCROW_PATH>`` (default ``.config/plx/offbox.key``) fails the run before any
upload. The first copy takes the first listed host that answers unless another has >10% more
free disk; it is then kept (``state.json``) and an unreachable kept host FAILS the run (exit
1, the alarm); it is never swapped for another. Free space is checked before any upload: the
Spark keeps ``PLX_OFFBOX_MIN_FREE_GB`` free after the whole remaining upload.

Bandwidth: ``PLX_OFFBOX_BWLIMIT_KBPS`` caps the upload over all streams together (KiB/s,
split evenly per stream; 0 = none), so a reseed leaves room for embedding and test traffic.
Partial uploads left in ``incoming/`` by an interrupted run are removed after
``PLX_OFFBOX_INCOMING_MAX_HOURS`` (default 12). A capture list file is copied only when the
frozen capture manifest names its digest (the piece's ``listing``) and the bytes read match.

Retention: the newest ``PLX_OFFBOX_KEEP`` records (the newest green one always kept);
objects no kept record names are removed, only when every kept record could be read.

``restore-test``: fetch the newest record from the Spark, decrypt it, download a sample
(every Postgres-set file, the capture manifest, then the smallest capture pieces and two
blob shards within ``PLX_OFFBOX_RESTORE_BYTES``), check each ciphertext digest, decrypt and
compare each plaintext digest with the record and with the source manifest's digest. When
the newest record does not pass, each older kept record is tried in turn until one does;
the output names the newest, the one that passed and every one tried. A corrupt record fails
the test even when an older one passes. Then the escrow (``PLX_OFFBOX_ESCROW_HOSTS``): the
escrowed key must equal ours and the escrowed backup secrets passphrase must open the
record's ``secrets-*.tar.gpg``, so recovery without this box is proven daily.

Excluded people: nothing new is copied: these are the backups already held on this box,
and the Spark holds them only as ciphertext it cannot read.

Runs alone (stdlib, ``python3 -I``) from /usr/local/lib/plx like capture_backup.py.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import gzip
import hashlib
import hmac
import json
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

CHUNK = 1 << 20
_HEX = re.compile(r"^[0-9a-f]{64}$")
_STAMP = re.compile(r"^manifest-(\d{8}T\d{6}Z)\.json$")
_RECORD = re.compile(r"^(\d{8}T\d{6}Z)\.json\.gpg$")
SSH_TIMEOUT = 60


class Fail(Exception):
    """A failed run: never green."""


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class Settings:
    def __init__(self) -> None:
        home = Path.home()
        # the data host and the key-escrow host are never the same machine (review
        # 459af42e5c): sb4ec holds the ciphertext, s7d3d the key's second copy
        self.hosts = _env("PLX_OFFBOX_HOSTS", "sb4ec").split()
        self.escrow_hosts = _env("PLX_OFFBOX_ESCROW_HOSTS", "s7d3d").split()
        self.escrow_path = _env("PLX_OFFBOX_ESCROW_PATH", ".config/plx/offbox.key")
        if not self.hosts or not all(re.fullmatch(r"[A-Za-z0-9._-]+", h) for h in self.hosts):
            raise Fail("hosts_invalid")
        both = sorted(set(self.hosts) & set(self.escrow_hosts))
        if both:
            raise Fail(f"data_host_is_escrow_host:{','.join(both)}")
        # the backup's secrets-archive passphrase, escrowed beside the key (never on a data host)
        self.secrets_pass_path = _env("PLX_OFFBOX_SECRETS_PASS_PATH", ".config/plx/backup-secrets.pass")
        for rel in (self.escrow_path, self.secrets_pass_path):
            if not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", rel) or ".." in rel.split("/"):
                raise Fail("escrow_path_invalid")
        self.ssh_config = _env("PLX_OFFBOX_SSH_CONFIG", "/data/plx-state/ts-petra/ssh_config")
        self.key = Path(_env("PLX_OFFBOX_KEY", str(home / ".config/plx/offbox.key")))
        self.remote_dir = _env("PLX_OFFBOX_REMOTE_DIR", "plx-offbox")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.remote_dir) or self.remote_dir in (".", ".."):
            raise Fail("remote_dir_invalid")
        self.keep = max(1, int(_env("PLX_OFFBOX_KEEP", "7")))
        self.min_free = int(float(_env("PLX_OFFBOX_MIN_FREE_GB", "200")) * 1e9)
        self.stage = Path(_env("PLX_OFFBOX_STAGE", "/data/plx-tmp/offbox-stage"))
        self.batch = int(float(_env("PLX_OFFBOX_BATCH_MB", "256")) * 1e6)
        self.max_seconds = float(_env("PLX_OFFBOX_MAX_SECONDS", "0"))
        self.backup_dir = Path(_env("PLX_BACKUP_DIR", "/data/backups")) / "daily"
        self.capture_dir = Path(_env("PLX_CAPTURE_BACKUP_DIR", "/var/backups/plx-capture"))
        self.state = Path(_env("PLX_OFFBOX_STATE", str(home / ".local/state/plx-offbox")))
        self.restore_bytes = int(float(_env("PLX_OFFBOX_RESTORE_MB", "600")) * 1e6)
        self.streams = min(12, max(1, int(_env("PLX_OFFBOX_STREAMS", "8"))))
        self.stagger = float(_env("PLX_OFFBOX_STAGGER", "2"))
        self.retries = max(0, int(_env("PLX_OFFBOX_RETRIES", "3")))
        self.pg_rescan = float(_env("PLX_OFFBOX_PG_RESCAN_SECONDS", "120"))
        # total upload cap over all streams in KiB/s (0: none); split evenly per stream
        self.bwlimit = max(0, int(_env("PLX_OFFBOX_BWLIMIT_KBPS", "0")))
        # interrupted partial uploads left in incoming/ are removed after this many hours
        self.incoming_hours = max(0.0, float(_env("PLX_OFFBOX_INCOMING_MAX_HOURS", "12")))
        # tests: a fake ssh/rsync; production: the system tools
        self.ssh = _env("PLX_OFFBOX_SSH_BIN", "ssh")
        self.rsync = _env("PLX_OFFBOX_RSYNC_BIN", "rsync")


# ---------------------------------------------------------------- key and encryption

def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise Fail(f"not_our_directory:{path}")
    os.chmod(path, 0o700)
    return path


def load_key(path: Path) -> bytes:
    """The key, refused unless a regular file of ours that nobody else may read."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Fail("key_missing (run: offbox_copy.py init-key)") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Fail("key_unsafe (must be our regular file, mode 0600)")
    key = path.read_bytes().strip()
    if len(key) < 43:
        raise Fail("key_short")
    return key


def init_key(path: Path) -> str:
    if path.exists():
        load_key(path)
        return "kept"
    private_dir(path.parent)
    import base64
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(os.urandom(32)) + b"\n")
    return "created"


class Crypto:
    def __init__(self, key_path: Path, work: Path) -> None:
        self.key_path = key_path
        self.key = load_key(key_path)
        self.home = private_dir(work / "gnupg")

    def object_name(self, plain_sha: str) -> str:
        return hmac.new(self.key, b"plx-offbox-object:" + plain_sha.encode(),
                        hashlib.sha256).hexdigest()

    def _gpg(self, *args: str) -> list[str]:
        return ["gpg", "--homedir", str(self.home), "--batch", "--quiet", "--yes",
                "--no-symkey-cache", "--pinentry-mode", "loopback",
                "--passphrase-file", str(self.key_path), *args]

    def encrypt(self, src: Path, dst: Path) -> tuple[str, str, int]:
        """Encrypt ``src`` into ``dst``; returns (plaintext sha256, ciphertext sha256, bytes)
        of what was read, so a file that changed under us is caught by its digest."""
        proc = subprocess.Popen(
            self._gpg("--symmetric", "--cipher-algo", "AES256", "--s2k-digest-algo", "SHA512",
                      "--compress-algo", "none", "-o", str(dst)),
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        plain = hashlib.sha256()
        assert proc.stdin is not None
        err: list[bytes] = []
        reader = threading.Thread(target=lambda: err.append(proc.stderr.read()))  # type: ignore[union-attr]
        reader.start()
        try:
            with open(src, "rb") as fh:
                for block in iter(lambda: fh.read(CHUNK), b""):
                    plain.update(block)
                    proc.stdin.write(block)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
        reader.join()
        if rc != 0:
            raise Fail(f"gpg_encrypt_failed:{src.name}:{rc}")
        os.chmod(dst, 0o600)
        return plain.hexdigest(), _sha256(dst), dst.stat().st_size

    def decrypt(self, src: Path, dst: Path) -> str:
        result = subprocess.run(self._gpg("--decrypt", "-o", str(dst), str(src)),
                                stdin=subprocess.DEVNULL, capture_output=True)
        if result.returncode != 0:
            raise Fail(f"gpg_decrypt_failed:{src.name}")
        return _sha256(dst)


# ---------------------------------------------------------------- remote

class Remote:
    """One Spark over ssh (one shared connection); fixed commands, hex names only."""

    def __init__(self, cfg: Settings, host: str, work: Path) -> None:
        self.cfg, self.host = cfg, host
        self.control = str(work / "ssh-%C")
        common = ["-F", cfg.ssh_config, "-o", "BatchMode=yes",
                  "-o", f"ConnectTimeout={SSH_TIMEOUT}", "-o", "ServerAliveInterval=30",
                  "-o", "ServerAliveCountMax=6"]
        self.base = common + ["-o", "ControlMaster=auto", "-o", f"ControlPath={self.control}",
                              "-o", "ControlPersist=120"]
        # one rsync stream per connection: no control socket, so each is its own TCP flow
        self.stream = common + ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
        self.dir = f"$HOME/{cfg.remote_dir}"
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.closed = False

    def _rsync(self, cmd: list[str], files: bytes, timeout: float) -> tuple[int, str]:
        with self._lock:
            if self.closed:
                raise Fail(f"run_stopping:{self.host}")
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            self._procs.add(proc)
        try:
            _, err = proc.communicate(files, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise Fail(f"rsync_timeout:{self.host}") from None
        finally:
            with self._lock:
                self._procs.discard(proc)
        tail = err.decode(errors="replace").strip().splitlines()[-1:] or [""]
        return proc.returncode, tail[0][:200]

    def close(self) -> None:
        """Stop every rsync still running (a run that unwinds); none starts after this."""
        with self._lock:
            self.closed = True
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass

    def run(self, script: str, stdin: bytes | None = None, timeout: float = 600) -> str:
        return self.run_bytes(script, stdin, timeout).decode()

    def run_bytes(self, script: str, stdin: bytes | None = None, timeout: float = 600) -> bytes:
        cmd = [self.cfg.ssh, *self.base, self.host, "bash -euo pipefail -c " + shlex.quote(script)]
        try:
            result = subprocess.run(cmd, input=stdin or b"", capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Fail(f"ssh_timeout:{self.host}") from None
        if result.returncode != 0:
            tail = result.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
            raise Fail(f"ssh_failed:{self.host}:{result.returncode}:{tail[0][:200]}")
        return result.stdout

    def _escrow_sh(self) -> str:
        k = f'"$HOME/{self.cfg.escrow_path}"'
        return f"if [ -e {k} ] || [ -L {k} ]; then echo ESCROW; exit 0; fi\n"

    def _refuse_escrow(self, out: str) -> None:
        if any(line == "ESCROW" for line in out.splitlines()):
            raise Fail(f"escrow_key_on_data_host:{self.host}")

    def escrow_check(self) -> None:
        """Refuse a configured data host that holds the key (~/.config/plx/offbox.key):
        ciphertext and key on one machine would make the copy readable there."""
        self._refuse_escrow(self.run(self._escrow_sh() + "echo CLEAR", timeout=120))

    def prepare(self) -> dict[str, Any]:
        """Make/check the private directory; returns free bytes and the object listing.
        Refused when the host holds the key, and unless every directory is ours, mode 0700,
        no symlink, and nothing under it is readable by anyone else. Partial uploads left in
        incoming/ longer than ``PLX_OFFBOX_INCOMING_MAX_HOURS`` are removed (no stream runs
        while this does: one run at a time, and prune calls it after every stream ended)."""
        mins = int(self.cfg.incoming_hours * 60)
        out = self.run(self._escrow_sh() + f"""
umask 077
d={self.dir}
[ ! -L "$d" ] || {{ echo SYMLINK; exit 9; }}
mkdir -p "$d/objects" "$d/copies" "$d/incoming"
chmod 700 "$d" "$d/objects" "$d/copies" "$d/incoming"
me=$(id -un)
echo "ME $me"
for x in "$d" "$d/objects" "$d/copies" "$d/incoming"; do
  [ ! -L "$x" ] || {{ echo SYMLINK; exit 9; }}
  echo "DIR $(stat -c '%U %a %F' "$x")"
done
echo "STALE $(find "$d/incoming" -mindepth 1 -maxdepth 1 -type f -mmin +{mins} -print -delete | wc -l)"
find "$d" -user "$me" ! -type l -perm /077 -exec chmod go= {{}} +
echo "LOOSE $(find "$d" \\( ! -user "$me" -o -perm /077 -o -type l \\) -print | wc -l)"
echo "FREE $(df -B1 --output=avail "$d" | tail -1 | tr -d ' ')"
cd "$d/objects" && find . -maxdepth 1 -type f -name '*.gpg' -printf 'OBJ %f %s\\n'
cd "$d/copies" && find . -maxdepth 1 -type f -name '*.json.gpg' -printf 'REC %f\\n'
""", timeout=300)
        self._refuse_escrow(out)
        me, free, loose, stale = None, None, None, 0
        objects: dict[str, int] = {}
        records: list[str] = []
        for line in out.splitlines():
            parts = line.split(" ")
            if parts[0] == "ME":
                me = parts[1]
            elif parts[0] == "DIR":
                if parts[1:4] != [me, "700", "directory"]:
                    raise Fail(f"remote_dir_unsafe:{self.host}:{' '.join(parts[1:])}")
            elif parts[0] == "LOOSE":
                loose = int(parts[1])
            elif parts[0] == "STALE":
                stale = int(parts[1])
            elif parts[0] == "FREE":
                free = int(parts[1])
            elif parts[0] == "OBJ" and parts[1].endswith(".gpg") and _HEX.match(parts[1][:-4]):
                objects[parts[1][:-4]] = int(parts[2])
            elif parts[0] == "REC" and _RECORD.match(parts[1]):
                records.append(parts[1])
        if me is None or free is None or loose is None:
            raise Fail(f"remote_prepare_unreadable:{self.host}")
        if loose:
            raise Fail(f"remote_dir_unsafe:{self.host}:{loose}_entries_not_private")
        return {"free": free, "objects": objects, "records": sorted(records),
                "incoming_removed": stale}

    def free(self) -> int:
        out = self.run("df -B1 --output=avail \"$HOME\" | tail -1", timeout=120)
        return int(out.strip())

    def _bwlimit(self, streams: int) -> list[str]:
        """rsync --bwlimit (KiB/s) per stream, so all streams together stay within the cap and
        a reseed leaves the tailnet to the embedding and test traffic."""
        if not self.cfg.bwlimit:
            return []
        return [f"--bwlimit={max(1, self.cfg.bwlimit // max(1, streams))}"]

    def send(self, stage: Path, names: list[str], sums: dict[str, str], dest: str) -> list[str]:
        """rsync the staged files to incoming/, then keep only those whose sha256 matches on
        the Spark, moved into ``dest``; returns the names that matched."""
        if not names:
            return []
        files = "".join(f"{n}\n" for n in names).encode()
        ssh = " ".join(shlex.quote(a) for a in [self.cfg.ssh, *self.stream])
        cmd = [self.cfg.rsync, "-t", "--checksum", "--partial", "--chmod=D700,F600",
               *self._bwlimit(self.cfg.streams),
               "--files-from=-", "-e", ssh, f"{stage}/", f"{self.host}:{self.cfg.remote_dir}/incoming/"]
        for attempt in range(self.cfg.retries + 1):
            code, tail = self._rsync(cmd, files, 24 * 3600)
            if code == 0:
                break
            if attempt == self.cfg.retries or self.closed:
                raise Fail(f"rsync_failed:{self.host}:{code}:{tail}")
            time.sleep(min(60, 5 * 2 ** attempt) * random.uniform(0.5, 1.5))
        listing = "".join(f"{sums[n]}  {n}\n" for n in names).encode()
        out = self.run(f"""
cd {self.dir}/incoming
{{ sha256sum -c - 2>/dev/null || true; }} | while IFS= read -r line; do
  case "$line" in
    *': OK') n="${{line%: OK}}"; chmod 600 "$n"; mv -f "$n" ../{dest}/"$n"; echo "MATCH $n";;
    *) echo "BAD $line"; rm -f "${{line%%:*}}";;
  esac
done
""", stdin=listing, timeout=3600)
        return [line[6:] for line in out.splitlines() if line.startswith("MATCH ")]

    def verify(self, sums: dict[str, str], where: str = "objects") -> set[str]:
        """sha256 of each named file on the Spark; returns the names that match."""
        if not sums:
            return set()
        listing = "".join(f"{s}  {n}\n" for n, s in sorted(sums.items())).encode()
        out = self.run(f"cd {self.dir}/{where} && {{ sha256sum -c - 2>/dev/null || true; }}",
                       stdin=listing, timeout=6 * 3600)
        return {line[:-4] for line in out.splitlines() if line.endswith(": OK")}

    def remove(self, names: list[str], where: str) -> None:
        for i in range(0, len(names), 500):
            part = [n for n in names[i:i + 500] if re.fullmatch(r"[0-9a-fTZ]+\.(gpg|json\.gpg)", n)]
            if part:
                self.run(f"cd {self.dir}/{where} && rm -f -- " + " ".join(part), timeout=600)

    def fetch(self, names: list[str], where: str, into: Path) -> None:
        if not names:
            return
        ssh = " ".join(shlex.quote(a) for a in [self.cfg.ssh, *self.base])
        cmd = [self.cfg.rsync, "-t", *self._bwlimit(1), "--files-from=-", "-e", ssh,
               f"{self.host}:{self.cfg.remote_dir}/{where}/", f"{into}/"]
        result = subprocess.run(cmd, input="".join(f"{n}\n" for n in names).encode(),
                                capture_output=True, timeout=12 * 3600)
        if result.returncode != 0:
            raise Fail(f"rsync_fetch_failed:{self.host}:{result.returncode}")


# ---------------------------------------------------------------- sources

def _healthy_set(directory: Path) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    """The newest healthy nightly set: (stamp, files, problems)."""
    stamps = sorted((m.group(1) for p in (directory.iterdir() if directory.is_dir() else [])
                     if (m := _STAMP.match(p.name))), reverse=True)
    for stamp in stamps:
        mpath = directory / f"manifest-{stamp}.json"
        try:
            manifest = json.loads(mpath.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or not manifest.get("files") \
                or manifest.get("pg_problem") or manifest.get("state_problems"):
            continue
        items = [{"group": "pg", "rel": f"daily/{name}", "path": str(directory / name),
                  "sha256": info["sha256"], "bytes": info["bytes"]}
                 for name, info in sorted(manifest["files"].items())]
        secrets = directory / f"secrets-{stamp}.tar.gpg"
        if secrets.is_file():  # encrypted already; listed under the config secrets
            items.append({"group": "pg", "rel": f"daily/{secrets.name}", "path": str(secrets),
                          "sha256": None, "bytes": secrets.stat().st_size})
        items.append({"group": "pg", "rel": f"daily/{mpath.name}", "path": str(mpath),
                      "sha256": None, "bytes": mpath.stat().st_size})
        return stamp, items, []
    return None, [], ["pg_set_missing"]


def _capture(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    try:
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        return {}, [], ["capture_manifest_unreadable"]
    problems = [] if manifest.get("complete") is True else ["capture_backup_incomplete"]
    problems += [f"capture_skipped:{k}" for k in sorted(manifest.get("skipped") or {})]
    items = [{"group": "capture", "rel": "capture/manifest.json", "path": str(root / "manifest.json"),
              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "frozen": raw}]
    # a list file is copied only when the frozen manifest names its digest (the piece's
    # ``listing``: sha256 of the uncompressed names); it is checked as read, in copy()
    digests = {f"{k.replace('/', '_')}.gz": (v or {}).get("listing")
               for k, v in (manifest.get("pieces") or {}).items()}
    lists = root / "lists"
    unverifiable = 0
    for p in sorted(lists.iterdir()) if lists.is_dir() else []:
        if not p.is_file() or p.is_symlink():
            continue
        digest = digests.get(p.name)
        if not (isinstance(digest, str) and _HEX.match(digest)):
            unverifiable += 1  # not needed to restore (the backup re-reads the archive)
            continue
        items.append({"group": "capture-lists", "rel": f"capture/lists/{p.name}",
                      "path": str(p), "sha256": None, "bytes": p.stat().st_size,
                      "listing": digest})
    rank = {"db": 0, "files.tar.zst": 1}
    pieces = []
    for key, info in (manifest.get("pieces") or {}).items():
        if ".." in key.split("/") or key.startswith("/"):
            problems.append(f"capture_piece_name_refused:{key}")
            continue
        pieces.append({"group": "capture", "rel": f"capture/{key}", "path": str(root / key),
                       "sha256": info.get("sha256"), "bytes": int(info.get("bytes") or 0),
                       "_rank": rank.get(key.split("/")[0], 2)})
    pieces.sort(key=lambda i: (i.pop("_rank"), -i["bytes"], i["rel"]))
    meta = {k: manifest.get(k) for k in ("complete", "completed_at", "format")}
    meta["lists_unverifiable"] = unverifiable
    return meta, items + pieces, problems


# ---------------------------------------------------------------- state

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _unreachable(exc: Fail, host: str) -> bool:
    return str(exc).startswith((f"ssh_failed:{host}:255:", f"ssh_timeout:{host}"))


def choose(cfg: Settings, work: Path, kept: str | None) -> tuple[Remote, dict[str, Any], list[str]]:
    """The kept Spark, or it fails: an unreachable kept Spark fails the run (exit 1, the
    alarm), it is never swapped for another one (review 459af42e5c: a fallback could put the
    data next to the key). Every reachable configured host is checked for the key first.
    Without a kept host (the first copy, or the kept one no longer configured) the first
    listed Spark that answers, unless another has >10% more free disk."""
    notes: list[str] = []
    seen: dict[str, tuple[Remote, dict[str, Any]]] = {}
    keep = kept if kept in cfg.hosts else None
    for host in cfg.hosts:
        remote = Remote(cfg, host, work)
        try:
            if keep and host != keep:
                remote.escrow_check()  # never copied to, but a configured host with the key fails
                continue
            seen[host] = (remote, remote.prepare())
        except Fail as exc:
            # only an unreachable non-kept Spark is passed over; one that answers with the
            # key or an unsafe directory (symlink, loose mode, not ours) fails the run
            if not _unreachable(exc, host):
                raise
            if host == keep:
                raise Fail(f"kept_host_unreachable:{exc}") from None
            notes.append(f"host_unusable:{exc}")
    if keep:
        return seen[keep][0], seen[keep][1], notes
    if not seen:
        raise Fail("no_spark_reachable: " + "; ".join(notes))
    # the listed order is the preference; another Spark only wins with clearly more free disk
    # (>10%), so a few bytes of difference cannot flip the choice between runs
    most = max(seen[h][1]["free"] for h in seen)
    host = next(h for h in cfg.hosts if h in seen and seen[h][1]["free"] * 1.1 >= most)
    if kept:
        notes.append(f"host_changed:{kept}->{host}")
    return seen[host][0], seen[host][1], notes


# ---------------------------------------------------------------- copy

def copy(cfg: Settings) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    state_dir = private_dir(cfg.state)
    state = _read_json(state_dir / "state.json", {})
    out: dict[str, Any] = {"stamp": stamp, "status": "failed"}
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="plx-offbox-") as tmp:
        work = private_dir(Path(tmp))
        crypto = Crypto(cfg.key, work)
        pg_stamp, pg_items, p1 = _healthy_set(cfg.backup_dir)
        cap_meta, cap_items, p2 = _capture(cfg.capture_dir)
        problems += p1 + p2
        items = pg_items + cap_items
        out.update(pg_set=pg_stamp, capture=cap_meta)
        if not items:
            raise Fail("nothing_to_copy: " + ",".join(problems))
        remote, info, notes = choose(cfg, work, state.get("host"))
        # another Spark unreachable is a note; losing the kept one (host_changed) is not green
        problems += [n for n in notes if not n.startswith("host_unusable:")]
        out["notes"] = notes
        out["host"] = remote.host
        out["incoming_removed"] = info.get("incoming_removed", 0)
        index: dict[str, dict[str, Any]] = state.get("objects", {}) if state.get("host") == remote.host else {}
        # an object the Spark no longer holds, or holds at another size, is sent again
        index = {n: v for n, v in index.items() if info["objects"].get(n) == v.get("bytes")}
        todo = [i for i in items if not (i.get("sha256") and crypto.object_name(i["sha256"]) in index)]
        need = sum(int(i["bytes"]) for i in todo)
        out.update(spark_free_bytes=info["free"], to_send_bytes=need)
        if info["free"] - need < cfg.min_free:
            raise Fail(f"spark_space:{remote.host}:free={info['free']},need={need},floor={cfg.min_free}")
        stage = private_dir(cfg.stage)
        for leftover in stage.iterdir():
            leftover.unlink()
        record: list[dict[str, Any]] = []
        sent = sent_bytes = launched = 0
        batch: list[tuple[dict[str, Any], str, str, str, int]] = []
        inflight: dict[Future, list[tuple[dict[str, Any], str, str, str, int]]] = {}
        stopped = False
        queue = deque(items)
        last_scan = time.monotonic()

        def collect(block: bool) -> None:
            nonlocal sent, sent_bytes
            if not inflight:
                return
            done, _ = wait(list(inflight), timeout=None if block else 0,
                           return_when=FIRST_COMPLETED)
            for fut in done:
                job = inflight.pop(fut)
                matched = set(fut.result())  # a stream that failed for good fails the run
                for item, name, plain, cipher, size in job:
                    (stage / f"{name}.gpg").unlink(missing_ok=True)
                    if f"{name}.gpg" in matched:
                        index[name] = {"cipher": cipher, "bytes": size}
                        sent += 1
                        sent_bytes += size
                    else:
                        problems.append(f"remote_checksum_mismatch:{item['rel']}")
            if done:
                state.update(host=remote.host, objects=index)
                _write_json(state_dir / "state.json", state)

        def flush() -> None:
            nonlocal launched
            if not batch:
                return
            job = list(batch)
            batch.clear()
            while len(inflight) >= cfg.streams:
                collect(True)
            pending = sum(b[4] for j in inflight.values() for b in j)
            if remote.free() - pending - sum(b[4] for b in job) < cfg.min_free:
                raise Fail(f"spark_space:{remote.host}")
            if 0 < launched < cfg.streams and cfg.stagger:
                time.sleep(cfg.stagger)  # the jump host's sshd drops a burst of new connections
            launched += 1
            names = sorted({f"{b[1]}.gpg" for b in job})
            sums = {f"{b[1]}.gpg": b[3] for b in job}
            inflight[pool.submit(remote.send, stage, names, sums, "objects")] = job
            collect(False)

        def newer_pg() -> None:
            """A newer healthy Postgres set goes to the head of the queue (owner 15 Nov: the
            Postgres backup is copied first, ahead of capture pieces)."""
            nonlocal pg_stamp, pg_items, items, queue
            stamp2, items2, _ = _healthy_set(cfg.backup_dir)
            if not stamp2 or (pg_stamp and stamp2 <= pg_stamp):
                return
            old = {i["rel"] for i in pg_items}
            record[:] = [r for r in record if r["rel"] not in old]
            items = items2 + [i for i in items if i["rel"] not in old]
            queue = deque(items2 + [i for i in queue if i["rel"] not in old])
            problems[:] = [x for x in problems if x != "pg_set_missing"]
            out.setdefault("pg_set_replaced", []).append(pg_stamp)
            pg_stamp, pg_items = stamp2, items2
            out["pg_set"] = pg_stamp

        pool = ThreadPoolExecutor(max_workers=cfg.streams, thread_name_prefix="offbox-send")
        try:
            while queue:
                if cfg.pg_rescan and time.monotonic() - last_scan >= cfg.pg_rescan:
                    last_scan = time.monotonic()
                    newer_pg()
                item = queue.popleft()
                if cfg.max_seconds:
                    spent = time.monotonic() - started
                    # an item that would not fit at the rate seen so far is left to the next run
                    ahead = int(item["bytes"]) / (sent_bytes / spent) if sent_bytes else 0
                    if spent + ahead > cfg.max_seconds:
                        stopped = True
                        break
                src = Path(item["path"])
                if "frozen" in item:  # the manifest as read at the start, not as it is now
                    src = work / "capture-manifest.json"
                    src.write_bytes(item["frozen"])
                elif "listing" in item:  # read once, checked, and exactly those bytes sent
                    try:
                        raw = src.read_bytes()
                        names = gzip.decompress(raw)
                    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
                        problems.append(f"source_unreadable:{item['rel']}")
                        continue
                    if hashlib.sha256(names).hexdigest() != item["listing"]:
                        problems.append(f"source_changed:{item['rel']}")
                        continue
                    src = work / "capture-list.gz"
                    src.write_bytes(raw)
                known = item.get("sha256") and crypto.object_name(item["sha256"])
                if known and known in index:
                    record.append({"rel": item["rel"], "sha256": item["sha256"], "object": known,
                                   "cipher": index[known]["cipher"], "bytes": item["bytes"]})
                    continue
                if not src.is_file():
                    problems.append(f"source_missing:{item['rel']}")
                    continue
                part = stage / "part.gpg"
                plain, cipher, size = crypto.encrypt(src, part)
                if item.get("sha256") and plain != item["sha256"]:
                    part.unlink()
                    problems.append(f"source_changed:{item['rel']}")
                    continue
                name = crypto.object_name(plain)
                held = index.get(name, {}).get("cipher") or next(
                    (b[3] for b in batch + [x for j in inflight.values() for x in j]
                     if b[1] == name), None)
                if held:
                    # same content as one already held, staged or in flight: gpg output is
                    # randomized, so the record names that object's ciphertext, not this one
                    part.unlink()
                    cipher = held
                else:
                    os.replace(part, stage / f"{name}.gpg")
                    batch.append((item, name, plain, cipher, size))
                record.append({"rel": item["rel"], "sha256": plain, "object": name,
                               "cipher": cipher, "bytes": item["bytes"]})
                if sum(b[4] for b in batch) >= cfg.batch:
                    flush()
                collect(False)
            flush()
            while inflight:
                collect(True)
        finally:
            remote.close()  # an unwinding run stops its streams before the stage is cleared
            pool.shutdown(wait=True, cancel_futures=True)
            for leftover in stage.iterdir():
                leftover.unlink()
            remote.closed = False  # the record is still sent below on a normal finish
        # every object this record names must match on the Spark now, not only when sent
        record = [r for r in record if r["object"] in index]
        want = {f"{r['object']}.gpg": r["cipher"] for r in record}
        ok = remote.verify(want)
        bad = sorted(r["rel"] for r in record if f"{r['object']}.gpg" not in ok)
        for r in record:
            if f"{r['object']}.gpg" not in ok:
                index.pop(r["object"], None)
        problems += [f"remote_verify_failed:{rel}" for rel in bad[:50]]
        if len(bad) > 50:
            problems.append(f"remote_verify_failed:+{len(bad) - 50}_more")
        record = [r for r in record if f"{r['object']}.gpg" in ok]
        covered = {r["rel"] for r in record}
        missing = [i["rel"] for i in items if i["rel"] not in covered]
        if stopped:
            problems.append(f"time_budget_reached:{len(missing)}_files_left")
        elif missing and not any(p.startswith(("source_", "remote_")) for p in problems):
            problems.append(f"files_not_copied:{len(missing)}")
        complete = not problems and not missing
        body = {"format": "plx-offbox/v1", "stamp": stamp, "host": remote.host,
                "complete": complete, "problems": problems, "pg_set": pg_stamp,
                "capture": cap_meta, "files": record, "missing": missing}
        rec_name = f"{stamp}.json.gpg"
        plain_rec = work / f"{stamp}.json"
        plain_rec.write_text(json.dumps(body, sort_keys=True))
        stage_rec = stage / rec_name
        _, rec_cipher, _ = crypto.encrypt(plain_rec, stage_rec)
        try:
            rec_ok = remote.send(stage, [rec_name], {rec_name: rec_cipher}, "copies") == [rec_name]
        finally:
            stage_rec.unlink(missing_ok=True)
        if not rec_ok:
            problems.append("record_checksum_mismatch")
        records = private_dir(state_dir / "records")
        _write_json(records / f"{stamp}.json", body | {"record_verified": rec_ok})
        state.update(host=remote.host, objects=index)
        _write_json(state_dir / "state.json", state)
        out["pruned"] = prune(cfg, remote, records, rec_ok and complete)
        green = complete and rec_ok
        out.update(status="green" if green else "incomplete", problems=problems,
                   files=len(items), files_copied=len(record), files_missing=len(missing),
                   sent_objects=sent, sent_bytes=sent_bytes, record=rec_name if rec_ok else None,
                   seconds=round(time.monotonic() - started))
        _write_json(state_dir / "status.json", out | {"finished_at": _now().isoformat()})
        return (0 if green else 3), out


def prune(cfg: Settings, remote: Remote, records: Path, this_green: bool) -> dict[str, Any]:
    """Keep the newest ``keep`` records (and the newest green one); remove objects no kept
    record names. Nothing is removed unless every kept record is in the local cache."""
    info = remote.prepare()
    names = info["records"]
    stamps = sorted((_RECORD.match(n).group(1) for n in names), reverse=True)  # type: ignore[union-attr]
    local = {s: _read_json(records / f"{s}.json", None) for s in stamps}
    green = [s for s in stamps if local[s] and local[s].get("complete") and local[s].get("record_verified")]
    keep = set(stamps[:cfg.keep]) | set(green[:1])
    drop = [s for s in stamps if s not in keep]
    if any(local[s] is None for s in keep):
        return {"skipped": "kept_record_not_in_local_cache"}
    if not green:
        # no green copy yet (the seed): records are dropped but objects are kept
        remote.remove([f"{s}.json.gpg" for s in drop], "copies")
        return {"records_removed": len(drop), "objects_removed": 0, "objects": "kept_until_green"}
    wanted = {f["object"] for s in keep for f in local[s]["files"]}
    gone = sorted(n for n in info["objects"] if n not in wanted)
    remote.remove([f"{s}.json.gpg" for s in drop], "copies")
    remote.remove([f"{n}.gpg" for n in gone], "objects")
    for s in drop:
        (records / f"{s}.json").unlink(missing_ok=True)
    return {"records_removed": len(drop), "objects_removed": len(gone)}


# ---------------------------------------------------------------- restore test

def _check_record(cfg: Settings, crypto: Crypto, remote: Remote, name: str, got: Path,
                  work: Path) -> dict[str, Any]:
    """Fetch one record, decrypt it and a sample of what it names; check each ciphertext and
    plaintext digest against the record and the sampled source manifests."""
    res: dict[str, Any] = {"record": name, "record_complete": None, "checked": 0,
                           "checked_bytes": 0, "problems": [], "secrets": []}
    problems: list[str] = res["problems"]
    try:
        remote.fetch([name], "copies", got)
        plain = work / "record.json"
        crypto.decrypt(got / name, plain)
        body = json.loads(plain.read_text())
        plain.unlink()
        files = body.get("files") or []
        res["record_complete"] = body.get("complete")
        if not files:
            raise Fail(f"record_empty:{name}")
        pick = [f for f in files if f["rel"].startswith("daily/") or f["rel"] == "capture/manifest.json"]
        budget = cfg.restore_bytes - sum(f["bytes"] for f in pick)
        rest = sorted((f for f in files if f not in pick and "/lists/" not in f["rel"]
                       and not f["rel"].startswith("capture/blobs/")), key=lambda f: f["bytes"])
        blobs = [f for f in files if f["rel"].startswith("capture/blobs/")]
        random.shuffle(blobs)
        for f in rest + blobs[:2]:
            if f["bytes"] <= budget:
                pick.append(f)
                budget -= f["bytes"]
        remote.fetch(sorted({f"{f['object']}.gpg" for f in pick}), "objects", got)
        for f in pick:
            enc = got / f"{f['object']}.gpg"
            if not enc.is_file() or _sha256(enc) != f["cipher"]:
                problems.append(f"cipher_mismatch:{f['rel']}")
                continue
            dst = work / "plain"
            if crypto.decrypt(enc, dst) != f["sha256"]:
                problems.append(f"plain_mismatch:{f['rel']}")
            elif re.fullmatch(r"daily/secrets-\d{8}T\d{6}Z\.tar\.gpg", f["rel"]):
                # the backup's own encrypted archive: opened with the escrowed passphrase later
                keep = work / f"secrets-{len(res['secrets'])}.tar.gpg"
                os.replace(dst, keep)
                res["secrets"].append((f["rel"], keep))
            dst.unlink(missing_ok=True)
        # the sampled source digests agree with the source manifests in the record
        bad = {f["rel"] for f in pick if any(p.endswith(":" + f["rel"]) for p in problems)}
        for m in (f for f in pick if f["rel"].startswith("daily/manifest-") and f["rel"] not in bad):
            enc, dst = got / f"{m['object']}.gpg", work / "m.json"
            crypto.decrypt(enc, dst)
            listed = json.loads(dst.read_text()).get("files", {})
            dst.unlink(missing_ok=True)
            for f in pick:
                fname = f["rel"].split("/", 1)[1]
                if fname in listed and listed[fname]["sha256"] != f["sha256"]:
                    problems.append(f"manifest_mismatch:{f['rel']}")
        res.update(checked=len(pick), checked_bytes=sum(f["bytes"] for f in pick))
    except (Fail, OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(f"record_unusable:{type(exc).__name__}:{str(exc)[:200]}")
    res["status"] = ("failed" if problems else
                     "green" if res["record_complete"] is True else "incomplete")
    return res


def _check_escrow(cfg: Settings, work: Path, secrets: list[tuple[str, Path]]) -> tuple[list[str], dict[str, Any]]:
    """Recovery without this box needs the escrowed copies: on each escrow host the off-box
    key (same sha256 as ours) and the backup secrets passphrase, both our regular files,
    0600, in a 0700 directory; the passphrase must open the record's secrets archive. Never
    printed: the passphrase goes from ssh stdout straight into a 0600 file under our 0700
    work directory and only gpg reads it."""
    problems: list[str] = []
    report: dict[str, Any] = {}
    if not cfg.escrow_hosts:
        return ["escrow_host_not_configured"], report
    local_key = hashlib.sha256(cfg.key.read_bytes()).hexdigest()
    for host in cfg.escrow_hosts:
        remote = Remote(cfg, host, work)
        k, ph = f'"$HOME/{cfg.escrow_path}"', f'"$HOME/{cfg.secrets_pass_path}"'
        try:
            out = remote.run(f"""
me=$(id -un)
for f in {k} {ph}; do
  if [ -f "$f" ] && [ ! -L "$f" ]; then
    echo "FILE $(stat -c '%U %a' "$f") $(stat -c '%U %a' "$(dirname "$f")") $(sha256sum < "$f" | cut -c1-64)"
  else echo "FILE missing"; fi
done
echo "ME $me"
""", timeout=120)
        except Fail as exc:
            problems.append(f"escrow_unreachable:{exc}")
            continue
        lines = out.splitlines()
        me = next((x.split(" ", 1)[1] for x in lines if x.startswith("ME ")), None)
        files = [x.split(" ")[1:] for x in lines if x.startswith("FILE ")]
        state: dict[str, str] = {}
        for label, parts in zip(("key", "secrets_pass"), files + [["missing"]] * 2):
            if parts == ["missing"]:
                state[label] = "missing"
            elif parts[:4] != [me, "600", me, "700"]:
                state[label] = "unsafe"
            elif label == "key" and parts[4] != local_key:
                state[label] = "different"
            else:
                state[label] = "ok"
        if state.get("secrets_pass") == "ok" and secrets:
            pw = work / "escrow-pass"
            try:
                fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(remote.run_bytes(f"cat {ph}", timeout=120))
                for rel, path in secrets:
                    opened = subprocess.run(
                        ["gpg", "--homedir", str(private_dir(work / "gnupg-escrow")), "--batch",
                         "--quiet", "--yes", "--no-symkey-cache", "--pinentry-mode", "loopback",
                         "--passphrase-file", str(pw), "--decrypt", "-o", os.devnull, str(path)],
                        stdin=subprocess.DEVNULL, capture_output=True)
                    if opened.returncode != 0:
                        state["secrets_pass"] = "does_not_open_archive"
                    else:
                        state.setdefault("opened", rel)
            except Fail as exc:
                state["secrets_pass"] = f"unreadable:{str(exc)[:100]}"
            finally:
                pw.unlink(missing_ok=True)
        report[host] = state
        problems += [f"escrow_{label}_{v}:{host}" for label, v in state.items()
                     if label != "opened" and v != "ok"]
    return problems, report


def restore_test(cfg: Settings) -> tuple[int, dict[str, Any]]:
    """The newest record first, then each older kept record until one passes; reports which.
    Green when one passes and no newer record was corrupt; a corrupt record (a digest that
    does not match, a record that cannot be read) fails the run even when an older one
    passes, so damage on the Spark always raises the alarm."""
    state = _read_json(private_dir(cfg.state) / "state.json", {})
    host = state.get("host")
    if not host:
        raise Fail("no_copy_yet")
    with tempfile.TemporaryDirectory(prefix="plx-offbox-restore-") as tmp:
        work = private_dir(Path(tmp))
        crypto = Crypto(cfg.key, work)
        remote = Remote(cfg, host, work)
        info = remote.prepare()
        if not info["records"]:
            raise Fail(f"no_record_on_spark:{host}")
        got = private_dir(work / "got")
        tried: list[dict[str, Any]] = []
        for name in reversed(info["records"]):
            tried.append(_check_record(cfg, crypto, remote, name, got, work))
            if tried[-1]["status"] == "green":
                break
        passed = next((t for t in tried if t["status"] == "green"), None)
        corrupt = [t for t in tried if t["status"] == "failed"]
        shown = passed or tried[0]
        # recovery without this box: the escrowed key and passphrase, the latter opening
        # the secrets archive of the record that passed
        escrow_problems, escrow = _check_escrow(cfg, work, shown["secrets"])
        out = {"host": host, "record": shown["record"], "record_complete": shown["record_complete"],
               "newest": tried[0]["record"], "passed": passed["record"] if passed else None,
               "checked": shown["checked"], "checked_bytes": shown["checked_bytes"],
               "tried": [{k: t[k] for k in ("record", "status", "problems")} for t in tried],
               "escrow": escrow,
               "secrets_archive_opened": bool(shown["secrets"]) and not escrow_problems,
               "problems": [f"{t['record']}:{p}" for t in corrupt for p in t["problems"]]
               + escrow_problems,
               "status": "failed" if corrupt or escrow_problems else
               "green" if passed else "incomplete"}
        _write_json(cfg.state / "restore-status.json", out | {"finished_at": _now().isoformat()})
        return (0 if out["status"] == "green" else 3 if out["status"] == "incomplete" else 1), out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("copy", "restore-test", "init-key"):
        print("usage: offbox_copy.py copy|restore-test|init-key", file=sys.stderr)
        return 2
    os.umask(0o077)
    # a stopped run (systemctl stop, TimeoutStartSec) unwinds: its staged files are removed
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        cfg = Settings()
        if args[0] == "init-key":
            print(json.dumps({"key": init_key(cfg.key), "path": str(cfg.key)}))
            return 0
        lock = os.open(private_dir(cfg.state) / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Fail("another_run_active") from None
        code, out = copy(cfg) if args[0] == "copy" else restore_test(cfg)
    except Fail as exc:
        out, code = {"status": "failed", "error": str(exc)}, 1
        try:
            _write_json(private_dir(Settings().state) / ("status.json" if args[0] == "copy"
                        else "restore-status.json"), out | {"finished_at": _now().isoformat()})
        except (Fail, OSError):
            pass
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
