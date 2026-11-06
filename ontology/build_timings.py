"""Every build step's start, end and duration, as the build runs (measurement only).

A generation build (``ontology.project_incremental.build_generation``) times its steps
into one :class:`BuildTimings`: the fingerprints, the seed, the projection, every
``post_project`` step, the commit's check and digest. Each step's start and end is logged
(``plx-build`` lines on ``log``: the host's is its stderr, its journal) and, once the build
has a generation directory, the timings so far are written to ``<generation>/timings.json``
at every step start and end, and handed to the caller's ``sink`` (the host keeps them in
its cycle row): a build that is stopped part way still says where its time went.

Nothing here is read back by a build, enters a unit key, a digest or the store: the
timings never change what is built.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Mapping

FILE = "timings.json"
FORMAT = "plx-build-timings/v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BuildTimings(dict):
    """step -> seconds (the build's ``seconds``), each assignment the end of that step:
    logged, kept with its wall-clock start and end, written to the generation directory
    (:meth:`attach`) and passed to ``sink``. :meth:`begin` logs a step's start."""

    def __init__(self, *, sink: Callable[[Mapping[str, Any]], None] | None = None,
                 log: Any = None) -> None:
        super().__init__()
        self.sink = sink
        self.log = log
        self.directory: Path | None = None
        self.started_at = _now()
        self._started = time.monotonic()
        self.steps: list[dict[str, Any]] = []
        self.running: dict[str, str] = {}

    def begin(self, name: str) -> None:
        self.running[name] = _now()
        self._say(f"start {name}")
        self._flush()

    def __setitem__(self, name: str, value: Any) -> None:
        super().__setitem__(name, value)
        ended = _now()
        started = self.running.pop(name, None)
        entry: dict[str, Any] = {"step": name, "seconds": value, "ended_at": ended}
        if started is not None:
            entry["started_at"] = started
        self.steps.append(entry)
        self._say(f"end {name} {value}s" if isinstance(value, (int, float)) else
                  f"end {name}")
        self._flush()

    def update(self, *args: Any, **kwargs: Any) -> None:   # every key through __setitem__
        for name, value in dict(*args, **kwargs).items():
            self[name] = value

    def attach(self, directory: Path) -> None:
        """Write the timings into ``directory`` from now on (the generation being built)."""
        self.directory = Path(directory)
        self._flush()

    def prefixed(self, prefix: str) -> "_Prefixed":
        """A dict whose assignments are this one's under ``prefix`` (a builder's own
        ``seconds``: its commit's check and digest)."""
        return _Prefixed(self, prefix)

    def snapshot(self) -> dict[str, Any]:
        return {"format": FORMAT, "started_at": self.started_at,
                "elapsed": round(time.monotonic() - self._started, 3),
                "running": dict(self.running), "steps": list(self.steps)}

    def _say(self, text: str) -> None:
        if self.log is None:
            return
        try:
            build = self.directory.name if self.directory is not None else "-"
            print(f"plx-build {_now()} gen={build} {text}", file=self.log, flush=True)
        except (OSError, ValueError):
            pass

    def _flush(self) -> None:
        snap = self.snapshot()
        if self.directory is not None and self.directory.is_dir():
            write_json(self.directory / FILE, snap)
        if self.sink is not None:
            try:
                self.sink(snap)
            except Exception:  # noqa: BLE001 - measurement never fails a build
                pass


class _Prefixed(dict):
    def __init__(self, parent: BuildTimings, prefix: str) -> None:
        super().__init__()
        self.parent, self.prefix = parent, prefix

    def __setitem__(self, name: str, value: Any) -> None:
        super().__setitem__(name, value)
        self.parent[self.prefix + name] = value

    def begin(self, name: str) -> None:
        self.parent.begin(self.prefix + name)


def write_json(path: Path, value: Any) -> None:
    """``value`` written whole or not at all (a temporary file renamed over ``path``);
    a write that fails keeps nothing (measurement never fails a build)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def read(directory: str | Path) -> dict[str, Any] | None:
    """The timings a build wrote into its generation directory, or None."""
    try:
        return json.loads((Path(directory) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


__all__ = ["BuildTimings", "FILE", "FORMAT", "read", "write_json"]
