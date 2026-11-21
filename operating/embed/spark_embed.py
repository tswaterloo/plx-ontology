"""The search follower's embedding on the Sparks' GPUs, with the box's CPU as the fallback.

``operating.embed follow --mode layers`` embeds the rows it just wrote for up to
``--embed-seconds`` a run; on the box that was a bge-m3 model on 4-8 CPU threads (up to
~10 CPU-minutes a run, 11-21) beside the API and Postgres. Here the same pending rows, in the
same order and through the same store path (``operating.embed.__main__.embed``), are encoded
by ``remote_worker.py`` on a GPU host the Spark health check calls fit for embedding
(``operating.spark_worker``: ``<state>/spark/health.json``, fresh, ``embed: true``) over
the same ssh pipe as ``operating.embed remote``. A host whose worker does not say the same
model, revision, sequence length and character cap (``remote.check_hello``) is not used.
No fit host, a host that does not start, or one failing part way: the run embeds on the
box's CPU as before (batches already stored stay stored). Only chunk texts go to the
Spark (the owner allows the served data there); nothing is written on it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from . import models as md

VERIFY_ROWS = 8        # stored rows embedded again on the host before it is used
MIN_COSINE = 0.999     # as ``operating.embed remote --min-cosine``
START_SECONDS = 90.0   # a worker that has not said hello by then (its model load) is dropped


def _started(host: Any, seconds: float) -> dict:
    """``host.start()`` within ``seconds`` (the ssh pipe has no time limit of its own: a
    host that hangs would hold the follower's run); past it the worker is killed."""
    import threading
    from . import remote
    out: dict = {}

    def go() -> None:
        try:
            out["hello"] = host.start()
        except BaseException as error:  # noqa: BLE001 - handed to the caller
            out["error"] = error
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        host.proc.kill()
        t.join(5)
        raise remote.HostFailed(f"{host.name}:start_timeout")
    if "error" in out:
        raise out["error"]
    return out["hello"]


def fit_hosts(state: Path, load: Optional[Callable[[Path], Any]] = None) -> list[str]:
    """The GPU hosts the last health check (at most ``spark_worker.MAX_AGE`` old) calls fit
    for embedding; none when the file is missing, old or unreadable (the box embeds)."""
    from operating import spark_worker
    found = (load or spark_worker.load_health)(Path(state))
    return spark_worker.fit_hosts(found, "embed") if found else []


def _args(state: Path, hosts: Sequence[str], ssh_config: Optional[str],
          local_cmd: Optional[Sequence[str]]) -> argparse.Namespace:
    from . import remote
    from operating import spark_worker
    import os
    argv = ["--state", str(state), "--ssh-config",
            ssh_config or os.environ.get(spark_worker.SSH_CONFIG_ENV)
            or spark_worker.DEFAULT_SSH_CONFIG]
    for host in hosts:
        argv += ["--host", host]
    for cmd in local_cmd or ():
        argv += ["--local-cmd", cmd]
    return remote.parser().parse_args(argv)


@contextmanager
def embedder(state: Path, model: str, column_dim: int, hosts: Sequence[str], *,
             conn: Any = None, ssh_config: Optional[str] = None,
             local_cmd: Optional[Sequence[str]] = None,
             log: Callable[[dict], None] = lambda _event: None) -> Iterator[Optional[md.Embedder]]:
    """An ``Embedder`` of ``model`` encoding on the first of ``hosts`` whose worker starts,
    says the same model as this box's (``remote.check_hello``) and, with ``conn``, gives
    ``VERIFY_ROWS`` stored rows back within ``MIN_COSINE`` of their stored vectors
    (``remote.verify``); closed after. None when none does (the caller embeds locally)."""
    from . import remote
    opened = None
    if hosts and model in md.LOCAL_MODELS:
        args = _args(state, hosts, ssh_config, local_cmd)
        args.model_name = model
        for index, name in enumerate(hosts):
            host = None
            try:
                if local_cmd:
                    import shlex
                    argv = shlex.split(local_cmd[index % len(local_cmd)])
                else:
                    remote.ship_worker(name, args)
                    argv = remote.ssh_argv(name, args)
                host = remote.Host(name, argv)
                differs = remote.check_hello(_started(host, START_SECONDS), model, column_dim)
                if differs is None and conn is not None:
                    sample = remote.stored_sample(conn, model, VERIFY_ROWS)
                    if sample:
                        least = remote.verify([host], sample, column_dim)[name]
                        if least < MIN_COSINE:
                            differs = "parity_below_min"
                if differs is not None:
                    log({"spark_embed": "host_refused", "why": differs})
                    host.close()
                    continue
                opened = host
                break
            except (remote.HostFailed, OSError, ValueError) as failed:
                log({"spark_embed": "host_failed", "why": str(failed).split(":")[-1][:40]})
                if host is not None:
                    host.close()
    if opened is None:
        yield None
        return
    try:
        yield md.Embedder(name=model, dim=int(opened.hello["dim"]), _encode=opened.encode,
                          remote=True)
    finally:
        opened.close()


def failed_on_host(error: BaseException) -> bool:
    """Whether ``error`` is the remote host failing (the run falls back to the box)."""
    from . import remote
    return isinstance(error, (remote.HostFailed, BrokenPipeError, EOFError))
