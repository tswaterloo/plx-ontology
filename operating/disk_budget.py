"""One disk budget with one floor (speed specs P3; master plan P3): every job that writes to
``/data`` asks here before it starts, for its peak (table + temp + WAL), and is granted or
refused with the numbers; no job keeps a floor of its own.

The state is a file, not Postgres (it must answer when Postgres is what is out of disk, and on
the Sparks): ``<PLX_STATE_DIR>/disk-budget.json`` (``/data/plx-state``), read and written under
``flock``. It holds the live reservations (job, GB, until); an expired one is dropped on every
call. Granted when ``free - FLOOR_GB - sum(live reservations) >= gb``.

``FLOOR_GB`` is the standby / WAL line: below it the standby's WAL and the served database are
at risk. ``PLX_DISK_FLOOR_GB_DATA`` (``operating.disk_floor``) may set it for one host.

CLI: ``python3 -m operating.disk_budget status | reserve JOB GB [TTL] | release JOB | check GB``
(``check`` exits 0 when GB could be granted now, 75 when not, and reserves nothing).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

FLOOR_GB = 60.0          # the standby / WAL line; the only floor
DEFAULT_TTL_S = 3600
STATE_NAME = "disk-budget.json"
PATH = "/data"


class Refused(RuntimeError):
    """The one refusal type (view_state classifies it ``disk``)."""

    def __init__(self, job: str, need_gb: float, available_gb: float):
        self.job, self.need_gb, self.available_gb = job, need_gb, available_gb
        super().__init__(f"disk budget refused {job}: needs {need_gb:.1f} GB, "
                         f"{available_gb:.1f} GB available above the {floor_gb():.0f} GB floor")


@dataclass(frozen=True)
class Granted:
    job: str
    gb: float
    until: float


def floor_gb(environ: Mapping[str, str] = os.environ) -> float:
    raw = (environ.get("PLX_DISK_FLOOR_GB_DATA") or "").strip()
    try:
        value = float(raw) if raw else FLOOR_GB
    except ValueError:
        raise RuntimeError(f"PLX_DISK_FLOOR_GB_DATA must be a number of GB, not {raw!r}")
    return value


def _state_file(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("PLX_STATE_DIR") or "/data/plx-state") / STATE_NAME


def free_gb(path: str = PATH) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


@contextlib.contextmanager
def _locked(environ: Mapping[str, str] = os.environ) -> Iterator[dict[str, Any]]:
    """The reservations, under an exclusive flock of the state file, written back on exit;
    expired ones dropped."""
    f = _state_file(environ)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read()
            state = json.loads(raw) if raw.strip() else {}
            if not isinstance(state, dict) or not isinstance(state.get("reservations"), dict):
                state = {"reservations": {}}
            now = time.time()
            state["reservations"] = {j: r for j, r in state["reservations"].items()
                                     if float(r.get("until", 0)) > now}
            yield state
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(state, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _available(state: Mapping[str, Any], free: float, floor: float, but: str = "") -> float:
    held = sum(float(r["gb"]) for j, r in state["reservations"].items() if j != but)
    return free - floor - held


def reserve(job: str, gb: float, ttl_s: int = DEFAULT_TTL_S, path: str = PATH,
            environ: Mapping[str, str] = os.environ) -> Granted:
    """Reserve ``gb`` for ``job`` (its own earlier reservation is replaced) or raise
    ``Refused`` with the need and what is available."""
    if gb < 0 or ttl_s <= 0:
        raise ValueError("a reservation is a positive size for a positive time")
    floor = floor_gb(environ)
    with _locked(environ) as state:
        available = _available(state, free_gb(path), floor, but=job)
        if available < gb:
            raise Refused(job, gb, available)
        until = time.time() + ttl_s
        state["reservations"][job] = {"gb": gb, "until": until}
    return Granted(job, gb, until)


def release(job: str, environ: Mapping[str, str] = os.environ) -> bool:
    with _locked(environ) as state:
        return state["reservations"].pop(job, None) is not None


def status(path: str = PATH, environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    floor = floor_gb(environ)
    free = free_gb(path)
    with _locked(environ) as state:
        res = {j: dict(r) for j, r in state["reservations"].items()}
        available = _available(state, free, floor)
    return {"path": path, "free_gb": round(free, 2), "floor_gb": floor,
            "reservations": res, "available_gb": round(available, 2)}


def check(gb: float, path: str = PATH, environ: Mapping[str, str] = os.environ) -> bool:
    return status(path, environ)["available_gb"] >= gb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="disk_budget")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("reserve")
    r.add_argument("job")
    r.add_argument("gb", type=float)
    r.add_argument("ttl", type=int, nargs="?", default=DEFAULT_TTL_S)
    rel = sub.add_parser("release")
    rel.add_argument("job")
    c = sub.add_parser("check")
    c.add_argument("gb", type=float)
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status()))
        return 0
    if args.cmd == "reserve":
        try:
            got = reserve(args.job, args.gb, args.ttl)
        except Refused as exc:
            print(json.dumps({"refused": str(exc)}))
            return 75
        print(json.dumps({"granted": got.job, "gb": got.gb, "until": got.until}))
        return 0
    if args.cmd == "release":
        print(json.dumps({"released": release(args.job)}))
        return 0
    ok = check(args.gb)
    print(json.dumps({"check_gb": args.gb, "fits": ok, **status()}))
    return 0 if ok else 75


if __name__ == "__main__":
    sys.exit(main())
