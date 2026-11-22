"""``python3 -m operating.embed remote --state D --host H [--host H2] [--dry-run]``: embed the
pending backlog of ``search.chunks`` on remote GPU hosts.

The backfill's work (``operating.embed.backfill``) with the model run elsewhere: every
content hash still without a vector of the configured local model is read here in
``content_sha`` order (a keyset walk over the ``chunks_content_sha`` index, one short
transaction per block, never the whole list in memory), its text (``header + "\\n\\n" +
body``, as ``store.texts_for``) shipped over an ``ssh`` pipe to
``operating/embed/remote_worker.py`` on each host, and the vectors brought back and
stored with ``store.store_embeddings`` (the backfill's write path: by hash, idempotent).

* **Same vectors in kind**: each host says which model, revision, sequence length and
  character cap it runs before any text goes out; a run whose host differs from the
  configured model and ``remote_worker``'s constants is refused. ``--verify N`` embeds N
  stored rows again on every host and reports the cosine to the stored vector (the
  run refuses below ``--min-cosine``).
* **Taking turns**: while it runs it holds the backfill's presence lock
  (``<state>/search/backfill.lock``): the follower leaves embedding to it and a CPU
  backfill does not start; another backfill holding it refuses this run.
* **Resumable**: a block's vectors are stored as they come back; the cursor
  (``<state>/search/remote_embed.json``) records the last hash below which every block
  is stored, and a new run starts after it (``--from-start``: from the first hash). A
  stored row is no longer pending, so a block stored twice is written once.
* **Hosts failing**: a host whose pipe ends or which answers an error is dropped and its
  unanswered blocks go to the other hosts; with no host left the run ends (exit 1) and
  is started again later; so does a run that stores nothing for ``--stall-seconds`` (a
  host that hangs). While the disk has less than ``--min-free-gb`` free, nothing new is
  sent (the run waits, it does not end).
* ``--dry-run`` checks the settings, the schema, the hosts' hello and (``--verify``) the
  parity, and counts what is pending (``--count``); it ships no pending text and writes
  nothing.

Prints JSON lines (counts, rates; never a text, a DSN or a key). Exit 0 done, 2 refused,
1 failed.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from . import config
from . import models as md
from . import remote_worker as rw
from . import store as st

CURSOR_NAME = Path("search") / "remote_embed.json"
WORKER_FILE = Path(__file__).with_name("remote_worker.py")
DEFAULT_REMOTE_DIR = "~/plx-embed"


# --- reading what is pending -------------------------------------------------------

def pending_texts(conn: Any, model: str, after: str, limit: int) -> list[tuple[str, str]]:
    """(content_sha, text) of the next ``limit`` hashes after ``after`` still without a
    vector of ``model`` (``store.pending``'s rule: words-only kinds are never embedded)."""
    from operating.pg import chunk_vectors
    rows = conn.execute(
        "SELECT DISTINCT ON (content_sha) content_sha, header, body FROM search.chunks "
        f"WHERE content_sha > %s AND {chunk_vectors.pending_predicate(conn)} "
        "AND kind <> ALL(%s::text[]) ORDER BY content_sha LIMIT %s",
        (after, model, list(st.WORDS_ONLY_KINDS), limit)).fetchall()
    conn.commit()
    return [(sha, header + "\n\n" + body) for sha, header, body in rows]


def count_pending(conn: Any, model: str) -> int:
    from operating.pg import chunk_vectors
    row = conn.execute(
        "SELECT count(DISTINCT content_sha) FROM search.chunks WHERE "
        f"{chunk_vectors.pending_predicate(conn)} AND kind <> ALL(%s::text[])",
        (model, list(st.WORDS_ONLY_KINDS))).fetchone()
    conn.commit()
    return int(row[0])


def stored_sample(conn: Any, model: str, limit: int) -> list[tuple[str, str, np.ndarray]]:
    from operating.pg import chunk_vectors
    sql = ("SELECT c.content_sha, c.header, c.body, v.embedding::text FROM "
           "search.chunk_vectors v JOIN search.chunks c ON c.kind = v.kind AND "
           "c.content_sha = v.content_sha WHERE v.embed_model = %s LIMIT %s"
           if chunk_vectors.narrow(conn) else
           "SELECT content_sha, header, body, embedding::text FROM search.chunks "
           "WHERE embed_model = %s AND embedding IS NOT NULL LIMIT %s")
    rows = conn.execute(sql, (model, limit)).fetchall()
    conn.commit()
    return [(sha, header + "\n\n" + body, np.asarray(json.loads(vec), dtype=np.float32))
            for sha, header, body, vec in rows]


# --- one host ----------------------------------------------------------------------

class HostFailed(RuntimeError):
    pass


class Host:
    """One worker process behind a pipe (``ssh host python remote_worker.py``)."""

    def __init__(self, name: str, argv: Sequence[str], log: Path | None = None) -> None:
        self.name = name
        self._log = open(log, "ab") if log else subprocess.DEVNULL
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._log)
        self.hello: dict[str, Any] = {}
        self.texts = 0
        self.seconds_busy = 0.0

    def start(self) -> dict[str, Any]:
        self.hello = self._header()
        if "error" in self.hello or "model" not in self.hello:
            raise HostFailed(f"{self.name}:bad_hello")
        return self.hello

    def _header(self) -> dict[str, Any]:
        try:
            frame = rw.read_frame(self.proc.stdout)
        except (OSError, ValueError, EOFError) as error:
            raise HostFailed(f"{self.name}:{type(error).__name__}") from error
        if frame is None:
            raise HostFailed(f"{self.name}:pipe_ended")
        return json.loads(frame.decode("utf-8"))

    def send(self, texts: Sequence[str]) -> None:
        try:
            rw.write_frame(self.proc.stdin, json.dumps({"texts": list(texts)}).encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as error:
            raise HostFailed(f"{self.name}:{type(error).__name__}") from error

    def receive(self, n: int) -> np.ndarray:
        head = self._header()
        if "error" in head:
            raise HostFailed(f"{self.name}:worker_error")
        dim = int(head.get("dim", 0))
        if head.get("n") != n or dim != self.hello.get("dim"):
            raise HostFailed(f"{self.name}:answer_shape")
        size = n * dim * 4
        raw = self.proc.stdout.read(size) if size else b""
        if len(raw) != size:
            raise HostFailed(f"{self.name}:answer_truncated")
        return np.frombuffer(raw, dtype="<f4").reshape(n, dim).astype(np.float32)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        self.send(texts)
        return self.receive(len(texts))

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write(struct.pack(">I", 0))
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self._log is not subprocess.DEVNULL:
            self._log.close()


def check_hello(hello: dict[str, Any], model: str, column_dim: int) -> str | None:
    """Why a host's worker would not make vectors identical in kind; None when it would."""
    want = {"model": model, "dim": md.LOCAL_MODELS.get(model),
            "max_seq_length": rw.MAX_SEQ_LENGTH, "max_chars": md.LOCAL_MAX_CHARS,
            "revision": rw.REVISIONS.get(model)}
    for key, value in want.items():
        if hello.get(key) != value:
            return f"host_{key}_differs"
    if int(hello["dim"]) > column_dim:
        return "embedding_column_narrower"
    return None


def ssh_argv(host: str, args: argparse.Namespace) -> list[str]:
    remote = (f"cd {args.remote_dir} && HF_HOME={args.remote_dir}/hf HF_HUB_OFFLINE=1 "
              # oom_score_adj 800: under memory pressure the worker goes before the host's
              # standby (11-22: an OOM on s7d3d took the standby); a pass resumes from its cursor
              f"exec choom -n 800 -- venv/bin/python remote_worker.py "
              f"--model {shlex.quote(args.model_name)} "
              f"--precision {args.precision} --batch {args.batch} "
              f"--memory-fraction {args.memory_fraction}")
    return [*_ssh_base(args), host, remote]


def _ssh_base(args: argparse.Namespace) -> list[str]:
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"]
    if args.ssh_config:
        base += ["-F", args.ssh_config]
    return base


def ship_worker(host: str, args: argparse.Namespace) -> None:
    """Put this repository's ``remote_worker.py`` on the host (the one that answers)."""
    done = subprocess.run([*_ssh_base(args), host,
                           f"mkdir -p {args.remote_dir} && cat > {args.remote_dir}/"
                           "remote_worker.py.tmp && mv "
                           f"{args.remote_dir}/remote_worker.py.tmp "
                           f"{args.remote_dir}/remote_worker.py"],
                          input=WORKER_FILE.read_bytes(), capture_output=True, timeout=120)
    if done.returncode != 0:
        raise HostFailed(f"{host}:ship_failed")


def open_hosts(args: argparse.Namespace, logs: Path | None) -> list[Host]:
    hosts = []
    for index, name in enumerate(args.host):
        if args.local_cmd:
            argv = shlex.split(args.local_cmd[index % len(args.local_cmd)])
        else:
            ship_worker(name, args)
            argv = ssh_argv(name, args)
        hosts.append(Host(name, argv, logs / f"remote-{name}.log" if logs else None))
    return hosts


def _stdout(event: Any) -> None:
    print(json.dumps(dict(event)), flush=True)


# --- the pass ----------------------------------------------------------------------

class Run:
    """Blocks read in hash order, split into length-sorted pages, sent to the hosts,
    stored by the writers; the cursor follows the stored blocks."""

    def __init__(self, args: argparse.Namespace, dsn: str, model: str, dim: int,
                 hosts: list[Host], cursor_path: Path, start_after: str) -> None:
        self.args, self.dsn, self.model, self.dim = args, dsn, model, dim
        self.hosts = hosts
        self.cursor_path = cursor_path
        self.after = start_after
        self.pages: queue.Queue = queue.Queue(maxsize=max(4, len(hosts) * args.inflight * 2))
        self.retry: collections.deque = collections.deque()
        self.stored: queue.Queue = queue.Queue(maxsize=max(4, args.writers * 4))
        self.lock = threading.Lock()
        self.blocks: dict[int, dict[str, Any]] = {}   # block -> {last, left}
        self.next_block = 0
        self.reading_done = False
        self.stop = threading.Event()
        self.failure: str | None = None
        self.alive = len(hosts)
        self.texts = self.rows = 0
        self.issued = self.finished = 0     # pages
        self.queued = 0                      # hashes read
        self.saved_after = start_after
        self.paused = False
        self._disk_at = -1e9
        self._low = False
        # the WAL budget (operating.pg.wal_budget): no request goes out while the standby
        # is too far behind; the disk is this pass's own --min-free-gb check
        from operating.pg import wal_budget
        self.budget = wal_budget.Budget.for_dsn(dsn, job="embed_remote", log=_stdout)
        self.ended: str | None = None

    # reader
    def read(self) -> None:
        conn = st.connect(self.dsn)
        try:
            after = self.after
            while not self.stop.is_set():
                if self.args.limit is not None and self.queued >= self.args.limit:
                    break
                want = self.args.block
                if self.args.limit is not None:
                    want = min(want, self.args.limit - self.queued)
                found = pending_texts(conn, self.model, after, want)
                if not found:
                    break
                after = found[-1][0]
                found.sort(key=lambda pair: len(pair[1]))
                pages = [found[i:i + self.args.page]
                         for i in range(0, len(found), self.args.page)]
                with self.lock:
                    block = self.next_block
                    self.next_block += 1
                    self.blocks[block] = {"last": after, "left": len(pages)}
                    self.issued += len(pages)
                    self.queued += len(found)
                for page in pages:
                    while not self.stop.is_set():
                        try:
                            self.pages.put((block, page), timeout=1)
                            break
                        except queue.Full:
                            continue
        except Exception as error:  # noqa: BLE001
            self.fail(f"read_failed:{type(error).__name__}")
        finally:
            conn.close()
            with self.lock:
                self.reading_done = True

    def low_disk(self) -> bool:
        """Whether no new request may go out: the store's disk has less free than
        ``--min-free-gb`` (read every 10 s; vectors and their index grow the disk) or the
        WAL budget is paused (the standby too far behind, or its lag unreadable)."""
        now = time.monotonic()
        if now - self._disk_at >= 10:
            self._disk_at = now
            free = shutil.disk_usage(self.args.free_path or self.args.state).free / 1e9
            low = bool(self.args.min_free_gb) and free < self.args.min_free_gb
            if low != self._low:
                print(json.dumps({"paused": "disk_below_min_free"} if low
                                 else {"resumed": round(free, 1)}), flush=True)
            self._low = low
        lagging = self.budget.blocked() is not None
        self.paused = self._low or lagging
        return self.paused

    def take(self) -> tuple[int, list[tuple[str, str]]] | None:
        if self.low_disk():
            time.sleep(0.5)
            return None
        with self.lock:
            if self.retry:
                return self.retry.popleft()
        try:
            return self.pages.get(timeout=0.5)
        except queue.Empty:
            return None

    # one host: a sender and a receiver, so a long answer never blocks the next request
    def drive(self, host: Host) -> None:
        inflight: collections.deque = collections.deque()
        slots = threading.Semaphore(self.args.inflight)
        broken = threading.Event()
        cond = threading.Condition()

        def receiver() -> None:
            while True:
                with cond:
                    while not inflight and not broken.is_set() and not self.stop.is_set():
                        cond.wait(0.5)
                    if not inflight:
                        return
                    item, sent_at = inflight[0]
                try:
                    vectors = host.receive(len(item[1]))
                except HostFailed:
                    broken.set()
                    return
                with cond:
                    inflight.popleft()
                host.texts += len(item[1])
                host.seconds_busy += time.monotonic() - sent_at
                slots.release()
                self.stored.put((item, vectors))

        thread = threading.Thread(target=receiver, daemon=True)
        thread.start()
        try:
            while not self.stop.is_set() and not broken.is_set():
                if not slots.acquire(timeout=0.5):
                    continue
                item = self.take()
                if item is None:
                    slots.release()
                    continue
                with cond:
                    inflight.append((item, time.monotonic()))
                    cond.notify()
                try:
                    # the worker cuts each text there too: only what it reads is shipped
                    host.send([text[:md.LOCAL_MAX_CHARS] for _, text in item[1]])
                except HostFailed:
                    broken.set()
        finally:
            thread.join(timeout=None if not broken.is_set() else 60)
            if broken.is_set():
                with cond, self.lock:
                    # unanswered pages go to the other hosts, oldest first
                    self.retry.extendleft(reversed([item for item, _ in inflight]))
                    inflight.clear()
                    self.alive -= 1
                    if self.alive == 0:
                        self.failure = self.failure or "no_host_left"
                        self.stop.set()
                print(json.dumps({"host_dropped": host.name}), flush=True)

    # writers
    def write(self) -> None:
        conn = st.connect(self.dsn)
        try:
            while True:
                try:
                    got = self.stored.get(timeout=0.5)
                except queue.Empty:
                    if self.stop.is_set():
                        return
                    continue
                if got is None:
                    return
                (block, page), vectors = got
                rows = self._store(conn, [sha for sha, _ in page], vectors)
                with self.lock:
                    self.rows += rows
                    self.texts += len(page)
                    self.finished += 1
                    self.blocks[block]["left"] -= 1
        except Exception as error:  # noqa: BLE001
            self.fail(f"write_failed:{type(error).__name__}")
        finally:
            conn.close()

    def _store(self, conn: Any, shas: list[str], vectors: np.ndarray) -> int:
        import psycopg
        for attempt in range(5):
            try:
                return st.store_embeddings(conn, self.model, self.dim, shas, vectors)
            except (psycopg.errors.DeadlockDetected,
                    psycopg.errors.SerializationFailure,
                    psycopg.errors.LockNotAvailable):
                conn.rollback()     # a follower wrote the same rows: take the turn again
                time.sleep(1 + attempt)
        return st.store_embeddings(conn, self.model, self.dim, shas, vectors)

    def fail(self, why: str) -> None:
        with self.lock:
            self.failure = self.failure or why
        self.stop.set()

    def advance_cursor(self) -> None:
        with self.lock:
            while self.blocks and self.blocks[min(self.blocks)]["left"] == 0:
                self.saved_after = self.blocks.pop(min(self.blocks))["last"]
        save_cursor(self.cursor_path, {"after": self.saved_after, "model": self.model})

    def finished_all(self) -> bool:
        with self.lock:
            return (self.reading_done and self.finished == self.issued
                    and not self.retry and self.pages.empty())

    def go(self) -> dict[str, Any]:
        started = time.monotonic()
        # the disk read before any driver's: a run that starts paused is paused from the
        # loop's first tick, never a stall because a loaded box ran no driver for
        # ``--stall-seconds`` (tests.test_embed_remote, 16 workers, 11-15)
        self.low_disk()
        threads = [threading.Thread(target=self.read, daemon=True)]
        threads += [threading.Thread(target=self.drive, args=(h,), daemon=True)
                    for h in self.hosts]
        writers = [threading.Thread(target=self.write, daemon=True)
                   for _ in range(self.args.writers)]
        for thread in threads + writers:
            thread.start()
        last_report = last_change = time.monotonic()
        seen = -1
        while not self.stop.is_set():
            time.sleep(0.2)
            if self.finished_all():
                break
            if self.args.max_seconds and time.monotonic() - started > self.args.max_seconds:
                self.ended = "max_seconds"      # in-flight answers are still stored
                break
            if self.finished != seen or self.paused:
                seen, last_change = self.finished, time.monotonic()
            elif time.monotonic() - last_change > self.args.stall_seconds:
                self.fail("stalled")       # nothing stored for that long: end, resume later
            if time.monotonic() - last_report >= self.args.report_seconds:
                self.advance_cursor()
                last_report = time.monotonic()
                print(json.dumps(self.progress(started)), flush=True)
        self.stop.set()
        if self.failure:
            for host in self.hosts:     # a hung host's answer is never waited for
                host.proc.kill()
        for thread in threads:
            thread.join(timeout=120)
        for thread in writers:
            thread.join()
        self.advance_cursor()
        report = self.progress(started)
        if self.failure:
            report["failed"] = self.failure
        if self.ended:
            report["ended"] = self.ended
        return report

    def progress(self, started: float) -> dict[str, Any]:
        seconds = time.monotonic() - started
        return {"texts": self.texts, "rows": self.rows, "seconds": round(seconds, 1),
                "texts_per_second": round(self.texts / seconds, 1) if seconds else 0,
                "hosts": {h.name: {"texts": h.texts} for h in self.hosts},
                "hosts_alive": self.alive, "cursor_set": self.saved_after != "",
                "paused": self.paused, "wal_budget": self.budget.report()}


def load_cursor(path: Path, model: str) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(value, dict) or value.get("model") != model:
        return ""
    after = value.get("after")
    return after if isinstance(after, str) else ""


def save_cursor(path: Path, cursor: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(cursor, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def verify(hosts: list[Host], sample: list[tuple[str, str, np.ndarray]], dim: int
           ) -> dict[str, float]:
    """The least cosine, per host, of its vector of each sampled text to the stored one
    (both as the column holds them: half precision, zero-padded to the column)."""
    out = {}
    stored = np.stack([v for _, _, v in sample])
    stored = stored / np.linalg.norm(stored, axis=1, keepdims=True)
    for host in hosts:
        got = host.encode([text for _, text, _ in sample])
        got = np.pad(got, ((0, 0), (0, dim - got.shape[1]))).astype(np.float16) \
            .astype(np.float32)
        got = got / np.linalg.norm(got, axis=1, keepdims=True)
        out[host.name] = round(float((got * stored).sum(axis=1).min()), 5)
    return out


def remote(args: argparse.Namespace) -> int:
    state = Path(args.state)
    values = config.settings()
    model = values.get("PLX_EMBED_MODEL") or config.DEFAULT_MODEL
    if model not in md.LOCAL_MODELS or model not in rw.LOCAL_MODELS:
        print(json.dumps({"refused": f"remote_is_for_local_models:{model}"}))
        return 2
    args.model_name = model
    dsn = values.get("PLX_PG_DSN") or config.DEFAULT_DSN
    conn = st.connect(dsn)
    try:
        return _remote(args, state, model, dsn, conn)
    finally:
        conn.close()


def _remote(args: argparse.Namespace, state: Path, model: str, dsn: str, conn: Any) -> int:
    try:
        st.require_schema(conn)
    except st.SchemaMissing as missing:
        print(json.dumps({"refused": str(missing)}))
        return 2
    dim = st.column_dim(conn)
    if dim is None or md.LOCAL_MODELS[model] > dim:
        print(json.dumps({"refused": "embedding_column_absent_or_narrower"}))
        return 2
    report: dict[str, Any] = {"model": model, "dry_run": bool(args.dry_run)}
    if args.count:
        report["pending_before"] = count_pending(conn, model)
    from .backfill import waited
    lock = state / "search" / "backfill.lock"
    with waited(lock, args.start_wait_seconds) if not args.dry_run else _nothing() as mine:
        if not mine:
            print(json.dumps({"refused": "another_backfill_running"}))
            return 2
        hosts: list[Host] = []
        try:
            try:
                hosts = open_hosts(args, state / "search" if not args.local_cmd else None)
                report["hosts"] = {h.name: h.start() for h in hosts}
            except HostFailed as error:
                print(json.dumps({"refused": f"host_unavailable:{error}"}))
                return 2
            for host in hosts:
                why = check_hello(host.hello, model, dim)
                if why:
                    print(json.dumps({"refused": f"{host.name}:{why}"}))
                    return 2
            if args.verify:
                sample = stored_sample(conn, model, args.verify)
                if sample:
                    report["verify_min_cosine"] = cosines = verify(hosts, sample, dim)
                    if min(cosines.values()) < args.min_cosine:
                        print(json.dumps(dict(report, refused="parity_below_min_cosine")))
                        return 2
            if args.dry_run:
                print(json.dumps(report, sort_keys=True))
                return 0
            report["reused"] = st.reuse_embeddings(conn, model)
            cursor_path = state / CURSOR_NAME
            start = "" if args.from_start else load_cursor(cursor_path, model)
            run = Run(args, dsn, model, dim, hosts, cursor_path, start)
            report.update(run.go())
        finally:
            for host in hosts:
                host.close()
    if args.count and "failed" not in report:
        report["pending_after"] = count_pending(conn, model)
    print(json.dumps(report, sort_keys=True), flush=True)
    return 1 if report.get("failed") else 0


class _nothing:
    def __enter__(self) -> bool:
        return True

    def __exit__(self, *exc: Any) -> None:
        return None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m operating.embed remote")
    p.add_argument("--state", required=True)
    p.add_argument("--host", action="append", required=True,
                   help="an ssh host (alias of --ssh-config) running the worker; repeat")
    p.add_argument("--ssh-config", help="ssh -F file (the hosts' proxy and known hosts)")
    p.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                   help="the hosts' directory holding venv/ and hf/ (the model cache)")
    p.add_argument("--precision", choices=("fp32", "fp16", "bf16"), default="fp16")
    p.add_argument("--batch", type=int, default=64, help="texts per model call on a host")
    p.add_argument("--memory-fraction", type=float, default=0.2,
                   help="the GPU memory share a host's worker may take")
    p.add_argument("--block", type=int, default=4096, help="hashes read per block")
    p.add_argument("--page", type=int, default=512, help="texts per request")
    p.add_argument("--inflight", type=int, default=3, help="requests in flight per host")
    p.add_argument("--writers", type=int, default=4, help="connections storing vectors")
    p.add_argument("--limit", type=int, help="embed at most this many hashes")
    p.add_argument("--from-start", action="store_true", help="ignore the cursor")
    p.add_argument("--dry-run", action="store_true",
                   help="check settings, hosts and parity; ship no pending text, write nothing")
    p.add_argument("--count", action="store_true", help="count pending hashes before/after")
    p.add_argument("--verify", type=int, default=20,
                   help="stored rows embedded again on each host to check parity (0: none)")
    p.add_argument("--min-cosine", type=float, default=0.999)
    p.add_argument("--report-seconds", type=float, default=30.0)
    p.add_argument("--start-wait-seconds", type=float, default=30.0)
    p.add_argument("--min-free-gb", type=float, default=55.0,
                   help="send nothing while the store's disk has less free (0: no check)")
    p.add_argument("--max-seconds", type=float,
                   help="end the run (exit 0, resumable) after this long")
    p.add_argument("--free-path", help="the disk to watch (default: --state)")
    p.add_argument("--stall-seconds", type=float, default=900.0,
                   help="end the run (exit 1) when nothing was stored for this long")
    p.add_argument("--local-cmd", action="append",
                   help=argparse.SUPPRESS)   # a worker run here instead of ssh (tests)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    return remote(parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
