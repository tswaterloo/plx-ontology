"""One process per host user that holds the search models (the query embedder and the
cross-encoder reranker, ``operating.search.models``) for every search on the host.

Each agent session's tool host used to load its own copy (about 3.4 GB resident, 5.2 GB at
peak, measured 2026-10-31): seven scoreboard workers with hybrid search on needed some
25-35 GB and, beside other runs, exhausted the host's memory. With ``PLX_SEARCH_MODELS``
unset or ``shared`` (``models.default_embedder`` / ``default_reranker``), every process
asks this one instead, and the first one that finds none starts it.

    python3 -m operating.search.model_server [--dir PATH] [--idle SECONDS]

- No network and no socket: requests and answers are JSON files in a spool directory only
  this user may open (0700, owned by the user, else refused), each written whole and then
  renamed into place; queries and passages are business text and never leave the user.
  The server holds ``start.lock`` while it lives, so one runs per directory and a client
  knows at once when none does.
- Requests are one JSON file each (``ping``, ``embed`` {texts}, ``rerank`` {query,
  passages, timeout}), bounded (``MAX_REQUEST`` bytes, ``MAX_TEXTS`` texts); answers one
  JSON line. The reranker keeps its time box and its one worker (``TimeBoxed``): a rerank
  that does not finish in time, or one asked while another runs, comes back without
  scores and the query is ranked without it, as before.
- It exits after ``IDLE_SECONDS`` without a request; the next search starts it again.

Nothing is logged but start, stop and errors by type; no text or score is written.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

DIR_ENV = "PLX_SEARCH_MODEL_DIR"
IDLE_SECONDS = 1800.0
MAX_REQUEST = 4 << 20          # bytes in one request
MAX_TEXTS = 200                # texts or passages in one request
START_SECONDS = 180.0          # how long a client waits for a server it started
POLL_SECONDS = 0.01
WORKERS = 8                    # requests answered at once (embeds queue on one lock)


def spool_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    given = env.get(DIR_ENV)
    return Path(given) if given else Path(f"/tmp/plx-search-models-{os.getuid()}")


def private_dir(folder: Path) -> Path:
    """The spool directory, created 0700; refused unless this user owns it and no one
    else may open it."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("model_dir_not_private")
    for sub in ("req", "resp"):
        (folder / sub).mkdir(mode=0o700, exist_ok=True)
    return folder


def running(folder: Path) -> bool:
    """Whether a server holds this directory's start lock now."""
    try:
        with open(folder / "start.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(payload, out)
    os.replace(tmp, path)


# ------------------------------------------------------------------ server

class _Models:
    def __init__(self) -> None:
        from operating.search import models
        self.embedder = models.local_embedder()
        self.reranker = models.local_reranker()
        self.embed_lock = threading.Lock()
        self.last = time.monotonic()

    def answer(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.last = time.monotonic()
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "embed_model": getattr(self.embedder, "model", None),
                    "reranker": None if self.reranker is None else bool(
                        getattr(self.reranker.reranker, "loaded", True))}
        if op == "embed":
            texts = request.get("texts")
            if self.embedder is None:
                return {"ok": False, "problem": "embedder_unavailable"}
            if not _strings(texts):
                return {"ok": False, "problem": "bad_request"}
            with self.embed_lock:
                return {"ok": True, "vectors": self.embedder.embed(texts)}
        if op == "rerank":
            passages, query = request.get("passages"), request.get("query")
            timeout = request.get("timeout")
            if self.reranker is None:
                return {"ok": True, "scores": None, "problem": "rerank_off"}
            if not _strings(passages, allow_empty=True) or not isinstance(query, str) \
                    or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
                return {"ok": False, "problem": "bad_request"}
            scores, problem = self.reranker.score(query, passages, float(timeout))
            return {"ok": True, "scores": scores, "problem": problem}
        return {"ok": False, "problem": "bad_request"}


def _strings(value: Any, allow_empty: bool = False) -> bool:
    return isinstance(value, list) and (allow_empty or value) and len(value) <= MAX_TEXTS \
        and all(isinstance(v, str) for v in value)


def serve(folder: Path, idle: float = IDLE_SECONDS) -> int:
    """Answer the spool's requests until ``idle`` seconds pass without one. Exits at once
    (0) when another server holds the directory."""
    private_dir(folder)
    lock = open(folder / "start.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return 0
    models = _Models()
    if models.reranker is not None:
        models.reranker.warm(wait=True)
    req, resp = folder / "req", folder / "resp"
    try:
        return _serve(models, req, resp, idle)
    finally:
        lock.close()


def _serve(models: "_Models", req: Path, resp: Path, idle: float) -> int:
    import concurrent.futures as cf

    def handle(work: Path, name: str) -> None:
        try:
            if work.stat().st_size > MAX_REQUEST:
                raise ValueError("too_large")
            out = models.answer(json.loads(work.read_text("utf-8")))
        except Exception as exc:  # noqa: BLE001 - answered by type, never a trace
            out = {"ok": False, "problem": "error:" + type(exc).__name__}
        finally:
            work.unlink(missing_ok=True)
        _write(resp / name, out)

    print("plx search models: serving", file=sys.stderr, flush=True)
    swept = time.monotonic()
    with cf.ThreadPoolExecutor(WORKERS, thread_name_prefix="plx-models") as pool:
        while time.monotonic() - models.last < idle:
            found = False
            try:
                entries = list(os.scandir(req))
            except FileNotFoundError:
                break   # the directory was removed: nothing can reach this server
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                work = req / (entry.name[:-5] + ".work")
                try:
                    os.replace(entry.path, work)   # claimed: this server answers it
                except OSError:
                    continue
                found = True
                pool.submit(handle, work, entry.name)
            if time.monotonic() - swept > 60:   # answers no client came back for
                swept = time.monotonic()
                for answer in resp.glob("*.json"):
                    try:
                        if time.time() - answer.stat().st_mtime > 300:
                            answer.unlink(missing_ok=True)
                    except OSError:
                        pass
            if not found:
                time.sleep(POLL_SECONDS)
    return 0


# ------------------------------------------------------------------ client

def request(payload: Mapping[str, Any], timeout: float, folder: Path | None = None
            ) -> dict[str, Any]:
    """One request to the server (OSError when none runs or none answers in time)."""
    folder = private_dir(folder or spool_dir())
    if not running(folder):
        raise ConnectionError("model_server_not_running")
    name = uuid.uuid4().hex + ".json"
    _write(folder / "req" / name, payload)
    answer = folder / "resp" / name
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                return json.loads(answer.read_text("utf-8"))
            finally:
                answer.unlink(missing_ok=True)
        time.sleep(POLL_SECONDS)
    (folder / "req" / name).unlink(missing_ok=True)   # not claimed yet: withdrawn
    raise TimeoutError("model_server_timeout")


def ensure(wait: float | None = None, folder: Path | None = None) -> dict[str, Any] | None:
    """The running server's ``ping`` answer, starting one when none holds the directory
    (a second one started at the same time exits at once); None when none answers
    within ``wait`` (default ``START_SECONDS``)."""
    folder = private_dir(folder or spool_dir())
    if not running(folder):
        env = dict(os.environ, PLX_SEARCH_MODELS="local")
        env[DIR_ENV] = str(folder)
        with open(folder / "server.log", "ab") as log:
            subprocess.Popen([sys.executable, "-m", "operating.search.model_server",
                              "--dir", str(folder)], env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True,
                             cwd=str(Path(__file__).resolve().parents[2]))
    deadline = time.monotonic() + (START_SECONDS if wait is None else wait)
    while time.monotonic() < deadline:
        try:
            return request({"op": "ping"}, max(1.0, deadline - time.monotonic()), folder)
        except (OSError, ValueError):
            time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=None)
    parser.add_argument("--idle", type=float, default=IDLE_SECONDS)
    args = parser.parse_args(argv)
    os.environ["PLX_SEARCH_MODELS"] = "local"
    return serve(args.dir or spool_dir(), args.idle)


if __name__ == "__main__":
    raise SystemExit(main())
