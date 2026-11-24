"""The box's side of the resident plan runner (``operator_api.plan_resident``): fetch the files it
wrote on the Spark and put their answers in place (``plan_sections.upsert``).

Each file is one record's answers under the stamps its run started at. A file is taken off the
Spark only after its answers were put (a failed put leaves it for the next round; a file that
fails alone, the rest being fine, is set aside in ``bad/`` so it cannot hold the others back). The
box reads files and writes its own Postgres; it asks the Spark for nothing else, and the ssh
login is the one the Spark runs already use. Names are checked (``^[A-Za-z0-9._-]+\\.jsonl$``)
before they reach a shell, and a file's lines go through the same checks as a batch file's
(``plan_sections.item``: anything unknown is not stored).

  python3 -m operating.pg.plan_sections_pull [--host s7d3d] [--dir DIR] [--every SECONDS] [--once]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Mapping, Protocol, Sequence

import psycopg

LOG = logging.getLogger("operating.pg.plan_sections_pull")
NAME = re.compile(r"^[A-Za-z0-9._-]+\.jsonl$")
DEFAULT_DIR = "plx-plan/out"           # under the Spark user's home
SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"
BATCH = 60                             # files put in one transaction
LEASE = ".lease"                       # ``operator_api.plan_resident.LEASE``
MAX_BYTES = 2_000_000
LOCK_KEY = "plx_plan_sections_pull"
MARKS_STALL_S = 60                     # under ``plan_sections.MARKS_FRESH_S``                  # a file bigger than this is not a record's answers


class Transport(Protocol):
    def names(self) -> list[str]: ...
    def read(self, names: Sequence[str]) -> dict[str, str]: ...
    def drop(self, names: Sequence[str], bad: bool = False) -> None: ...


class DirTransport:
    """Files in a local folder (a test, or a Spark folder mounted here)."""

    def __init__(self, folder: str, max_bytes: int | None = MAX_BYTES) -> None:
        self.folder, self.max_bytes = folder, max_bytes

    def names(self) -> list[str]:
        with open(os.path.join(self.folder, LEASE), "a"):
            os.utime(os.path.join(self.folder, LEASE))
        return sorted(n for n in os.listdir(self.folder) if NAME.match(n))

    def read(self, names: Sequence[str]) -> dict[str, str]:
        out = {}
        for n in names:
            if NAME.match(n) and (self.max_bytes is None or
                                  os.path.getsize(os.path.join(self.folder, n)) <= self.max_bytes):
                with open(os.path.join(self.folder, n), encoding="utf-8") as fh:
                    out[n] = fh.read()
        return out

    def drop(self, names: Sequence[str], bad: bool = False) -> None:
        for n in names:
            if NAME.match(n):
                if bad:
                    os.makedirs(os.path.join(self.folder, "bad"), exist_ok=True)
                    os.replace(os.path.join(self.folder, n), os.path.join(self.folder, "bad", n))
                else:
                    os.remove(os.path.join(self.folder, n))


class SshTransport:
    """The folder on a Spark, over the ssh login the Spark runs use (one round trip per
    operation, however many files)."""

    def __init__(self, host: str, folder: str = DEFAULT_DIR, config: str | None = None,
                 run: Any = None, max_bytes: int | None = MAX_BYTES) -> None:
        self.max_bytes = max_bytes      # None: every file, whatever its size
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host) or not re.fullmatch(r"[A-Za-z0-9._/-]+", folder):
            raise ValueError("host and folder are plain names")
        self.host, self.folder = host, folder
        self.config = config or os.environ.get("PLX_SPARK_SSH_CONFIG", SSH_CONFIG)
        self._run = run or self._ssh

    def _ssh(self, script: str) -> str:
        got = subprocess.run(["ssh", "-F", self.config, "-o", "BatchMode=yes", "-o",
                              "ConnectTimeout=10", self.host, script], capture_output=True,
                             text=True, timeout=120)
        if got.returncode:
            raise RuntimeError(f"ssh {self.host}: {got.stderr.strip()[-200:]}")
        return got.stdout

    def names(self) -> list[str]:
        # the same round trip tells the runner someone is fetching (its lease: it ends without)
        out = self._run(f"cd {shlex.quote(self.folder)} 2>/dev/null && touch {LEASE} && "
                        "ls -1 *.jsonl 2>/dev/null; true")
        return sorted(n for n in out.split() if NAME.match(n))

    def read(self, names: Sequence[str]) -> dict[str, str]:
        names = [n for n in names if NAME.match(n)]
        if not names:
            return {}
        mark = "=====PLXFILE====="
        script = (f"cd {shlex.quote(self.folder)} && for f in {' '.join(shlex.quote(n) for n in names)}; "
                  + (f"do if [ \"$(stat -c %s \"$f\")\" -le {int(self.max_bytes)} ]; then "
                     if self.max_bytes is not None else "do if true; then ")
                  + f"echo '{mark} '\"$f\"; cat \"$f\"; fi; done")
        out, got, name = self._run(script), {}, None
        for line in out.splitlines(keepends=True):
            if line.startswith(mark + " "):
                name = line[len(mark) + 1:].strip()
                got[name] = ""
            elif name is not None:
                got[name] += line
        return {n: v for n, v in got.items() if n in names}

    def put_ids(self, ids: Sequence[str], name: str) -> None:
        """Hand record ids to the runner: one ``.ids`` file in its queue folder (the sibling
        ``queue`` of the answers' ``out``), written whole then renamed into place."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.ids", name):
            raise ValueError("a plain file name")
        queue = re.sub(r"/out/?$", "/queue", self.folder)
        body = "".join(i + "\n" for i in ids if "\n" not in i)
        self._run(f"mkdir -p {shlex.quote(queue)} && printf %s {shlex.quote(body)} > "
                  f"{shlex.quote(queue + '/.' + name)} && mv -f {shlex.quote(queue + '/.' + name)} "
                  f"{shlex.quote(queue + '/' + name)}")

    def drop(self, names: Sequence[str], bad: bool = False) -> None:
        names = [n for n in names if NAME.match(n)]
        if not names:
            return
        quoted = " ".join(shlex.quote(n) for n in names)
        move = f"mkdir -p bad && mv -f {quoted} bad/" if bad else f"rm -f {quoted}"
        self._run(f"cd {shlex.quote(self.folder)} && {move}")


PLAN_ENV = "/etc/plx/plan-resident.env"


def runner_hosts(path: str = PLAN_ENV) -> list[str]:
    """The hosts the resident shards run on: ``PLX_PLAN_HOSTS`` (space separated) of the runner's
    own settings file, the one deploy/spark/plan_resident.sh reads, so the pull and the runners
    cannot disagree. Absent or empty: s7d3d only (as before). A name that is not a plain host
    name raises: a typo must not silently leave a host's answers unfetched."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:          # no settings file: the single-host setup of before
        return ["s7d3d"]
    hosts = ["s7d3d"]
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        value = value.strip().strip("\"'")      # the file is sourced by sh: its value is quoted
        if sep and key == "PLX_PLAN_HOSTS" and value.split():
            hosts = value.split()
    for h in hosts:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", h):
            raise ValueError(f"PLX_PLAN_HOSTS has {h!r}, not a host name")
    return hosts


class FanOut:
    """Page requests go to every runner host's queue (each shard takes the ids its hash owns, and
    which host runs a shard is the unit's business, not the puller's). All hosts must accept
    before the spool is cleared: a host that is away raises, the requests stay in the spool, and
    the hosts that did take them see the same ids again next round (a repeat is harmless: a
    record asked twice is computed twice)."""

    def __init__(self, transports: Sequence[Any]) -> None:
        self.transports = list(transports)

    def put_ids(self, ids: Sequence[str], name: str) -> None:
        errors = []
        for t in self.transports:
            try:
                t.put_ids(ids, name)
            except Exception as exc:  # noqa: BLE001 - raised below once every host was tried
                errors.append(f"{getattr(t, 'host', '?')}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))


def parse(text: str) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], Mapping[str, Any]]:
    """A file's stamps (its first line), answers and the rest of its header; anything else
    raises (the file is bad)."""
    lines = [json.loads(x) for x in text.splitlines() if x.strip()]
    if not lines or not isinstance(lines[0], dict) or not isinstance(lines[0].get("stamps"), dict):
        raise ValueError("no stamps line")
    rest = [x for x in lines[1:] if isinstance(x, dict) and "record_id" in x]
    return lines[0]["stamps"], rest, {k: v for k, v in lines[0].items() if k != "stamps"}


def send_requests(transport: Any, spool: str | None = None) -> int:
    """Pass the pages' waiting requests (``operating.plan_requests``) to the runner's queue and
    take them off the spool; a request not delivered stays for the next round. How many went."""
    from operating import plan_requests
    got = plan_requests.waiting(spool)
    if not got:
        return 0
    transport.put_ids([rid for _n, rid in got], f"req-{int(time.time() * 1000)}.ids")
    plan_requests.done([n for n, _rid in got], spool)
    return len(got)


def pull_once(dsn: str, transport: Transport, schema: str | None = None) -> dict[str, Any]:
    """Fetch every waiting file (up to ``BATCH`` per transaction), put the answers in place and
    take the files off the Spark. A failure of the whole batch is retried file by file."""
    from operating.pg import plan_sections
    names = transport.names()
    total = {"files": 0, "placed": 0, "replaced": 0, "older": 0, "skipped": 0, "bad": 0,
             "moved_s": []}
    for at in range(0, len(names), BATCH):
        chunk = names[at:at + BATCH]
        texts = transport.read(chunk)
        parsed: dict[str, Any] = {}
        heads: dict[str, Mapping[str, Any]] = {}
        for n in chunk:
            try:
                stamps, answers, heads[n] = parse(texts[n])
                parsed[n] = (stamps, answers)
            except (KeyError, ValueError):
                transport.drop([n], bad=True)
                total["bad"] += 1
        groups = [n for n in chunk if n in parsed]
        if not groups:
            continue
        try:
            got = plan_sections.upsert(dsn, [parsed[n] for n in groups], schema)
        except psycopg.errors.LockNotAvailable:
            # a table is being swapped: nothing is wrong with the files, they stay for the next round
            LOG.info("plan sections: a table is locked by a rebuild; the files wait")
            total["waited"] = total.get("waited", 0) + len(groups)
            return total
        except Exception as exc:  # noqa: BLE001 - tried again one file at a time
            LOG.warning("plan sections: a batch of %d files failed (%s); one by one", len(groups), exc)
            for n in groups:
                try:
                    one = plan_sections.upsert(dsn, [parsed[n]], schema)
                except psycopg.errors.LockNotAvailable:
                    total["waited"] = total.get("waited", 0) + 1
                    return total
                except Exception as err:  # noqa: BLE001 - this file alone is bad
                    LOG.warning("plan sections: %s set aside: %s", n, err)
                    transport.drop([n], bad=True)
                    total["bad"] += 1
                    continue
                _add(total, one)
                transport.drop([n])
                total["files"] += 1
                _latency(total, heads.get(n))
            continue
        _add(total, got)
        transport.drop(groups)
        total["files"] += len(groups)
        for n in groups:
            _latency(total, heads.get(n))
    return total


def _latency(total: dict[str, Any], head: Mapping[str, Any] | None, now: float | None = None) -> None:
    """For a record whose row had gone stale (its dependencies moved): seconds from the runner
    finding it to this put, plus the standby's replay lag then (how long the standby was behind
    the box). The change itself is older than this by up to the time the runner took to look."""
    seen = (head or {}).get("seen_at")
    if isinstance(seen, (int, float)) and "moved" in ((head or {}).get("why") or {}).values():
        lag = (head or {}).get("replay_lag_s")
        total["moved_s"].append(round((time.time() if now is None else now) - seen
                                      + (lag if isinstance(lag, (int, float)) else 0), 1))


def _add(total: dict[str, Any], got: Mapping[str, Any]) -> None:
    for k in ("placed", "replaced", "older", "skipped"):
        total[k] += got.get(k, 0)
    total["db_s"] = round(total.get("db_s", 0.0) + float(got.get("seconds", 0.0)), 2)


def singleton(dsn: str) -> Any:
    """A connection holding the puller's advisory lock for the life of the process, None when
    another puller holds it (two pullers would race on the same files)."""
    conn = psycopg.connect(dsn, autocommit=True)
    if not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,)).fetchone()[0]:
        conn.close()
        return None
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.plan_sections_pull")
    ap.add_argument("--host", action="append",
                    help="a runner host (repeat for several; default PLX_PLAN_HOSTS of the runner's settings file, else s7d3d): each host's folder is "
                         "pulled, and page requests go to every host's queue")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    transports = [SshTransport(h, args.dir) for h in dict.fromkeys(args.host or runner_hosts())]
    transport = FanOut(transports)
    held = singleton(dsn)
    if held is None:
        LOG.error("plan sections pull: another puller holds the lock; this one ends")
        return 0
    try:
        idle = 0
        marked = time.monotonic()
        while True:
            try:
                from operating.pg import plan_sections
                plan_sections.mark_links(dsn)    # the hubs' newest-link marks (rows naming a hub need them)
                marked = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - hub rows then read live once the marks age out
                LOG.warning("plan sections: the link marks round failed: %s", exc)
                if time.monotonic() - marked > MARKS_STALL_S:
                    # the hub rows go live at MARKS_FRESH_S; fail the unit, so the host's own alarm
                    # (OnFailure of every plx- unit) says so, and systemd starts it again
                    LOG.error("plan sections pull: no marks round for %d s; failing", MARKS_STALL_S)
                    return 1
            try:
                sent = send_requests(transport)
                if sent:
                    LOG.info("plan sections: %d page requests passed to the runner", sent)
            except Exception as exc:  # noqa: BLE001 - they wait in the spool for the next round
                LOG.warning("plan sections: page requests not passed on: %s", exc)
            for one in transports:         # a host that is away never holds back the others
                try:
                    got = pull_once(dsn, one)
                except Exception as exc:  # noqa: BLE001 - the Spark or the database is away: try again
                    LOG.warning("plan sections pull from %s failed: %s", one.host, exc)
                    got = None
                if got and got["files"]:
                    idle = 0
                    moved = got.pop("moved_s")
                    got["found_to_put_s"] = (f"min {min(moved)} median {sorted(moved)[len(moved) // 2]} "
                                               f"max {max(moved)}" if moved else "")
                    LOG.info("plan sections put (%s): %s", one.host, json.dumps(got))
            if args.once:
                return 0
            idle += 1
            time.sleep(args.every if idle < 12 else min(args.every * 3, 30))

    finally:
        held.close()

if __name__ == "__main__":
    sys.exit(main())
