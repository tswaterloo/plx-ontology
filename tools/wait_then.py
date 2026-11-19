"""Wait for exact conditions, check the target host has room, then run a command.

One tested replacement for the per-job shell waiters under /data/scratch, which on 11-17
started jobs early three times (a waiter matched a quoted mention of its phrase in another
MASTER line, its own line quoting the rule, a "PENDING ... until" line), and for GPU
launches made without a memory check (both Sparks hung, 11-17 6:30 AM ET).

    python3 tools/wait_then.py [conditions] [pre-flight] [--timeout 6h] -- <command> [args...]

Conditions (all must hold; each is latched once seen; at least one, or --now):
  --master FILE --marker '[[REBUILD-LIVE-DONE]]'
        a line of FILE that starts with "- ", carries the marker exactly (a ``[[...]]`` token
        of capitals, digits and dashes; free text is refused), not inside quotes or
        backticks, and with no word saying it has not happened (pending, until, waiting,
        when, once, if, will, should, propose, expect, before, not yet, ...). A line already
        there when the wait starts never counts (--accept-existing to allow), so an old or
        quoted marker cannot start the job; the file may be rewritten (tmp+mv, prepends).
  --unit NAME[:STATE]   a systemd unit (``--user-unit`` for the user manager). STATE:
        success (default: the unit ran after the wait began and ended with Result=success),
        done (ran and ended, any result), active, failed. An unknown unit refuses at once
        (systemd calls it "inactive"); a unit awaited for success that fails ends the wait
        with exit 7.
  --file PATH           PATH exists and is non-empty, and was created or changed after the
        wait began (--accept-existing: already there is enough).
Pre-flight (checked once the conditions hold, re-checked each interval until --timeout):
  --host NAME (default: this box; else ssh, BatchMode), --min-ram-gb N (MemAvailable),
  --min-disk-gb N [--disk-path P], --min-gpu-free-gb N (nvidia-smi memory.free; on a
  unified-memory GPU that reports [N/A], such as the DGX Spark's, MemAvailable is the GPU's).
  A host it cannot reach or a figure it cannot read refuses (fail closed).
Exit: the command's (exec), 3 usage/unknown unit, 4 pre-flight not met by --timeout,
5 conditions not met by --timeout, 6 host unreachable/unreadable, 7 awaited unit failed.
Times printed are US Eastern.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no tz database
    ET = None

MARKER = re.compile(r"^\[\[[A-Z0-9][A-Z0-9-]{2,63}\]\]$")
# a line that says the marker has NOT been reached (yet): never a done line
NEGATING = re.compile(
    r"\b(pending|until|wait|waits|waiting|waiter|awaiting|when|once|if|will|would|should|"
    r"shall|propose|proposed|proposal|convention|expect|expected|expects|before|planned|"
    r"todo|not yet|blocked|tbd|eta|queued|armed|post|posts|posting)\b", re.I)
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`|“[^”]*”|‘[^’]*’")
DEFAULT_SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"
UNIT_STATES = ("success", "done", "active", "failed")


class Stop(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def now_et() -> str:
    t = datetime.now(ET) if ET else datetime.now()
    return t.strftime("%-I:%M:%S %p ET %m-%d")


def say(msg: str) -> None:
    print(f"wait_then {now_et()}: {msg}", file=sys.stderr, flush=True)


# --- MASTER marker ---------------------------------------------------------------------------

def marker_line(line: str, marker: str) -> bool:
    """A done line: starts with "- ", carries the marker outside quotes, negates nothing."""
    if not line.startswith("- ") or marker not in line:
        return False
    if marker not in QUOTED.sub(" ", line):
        return False   # only quoted mentions
    # the marker must stand alone (not part of a longer token like [[X-DONE]]-ish text)
    if not re.search(r"(^|[\s(,;:])" + re.escape(marker) + r"($|[\s),;:.])", line):
        return False
    return NEGATING.search(QUOTED.sub(" ", line).replace(marker, " ")) is None


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()


@dataclass
class MasterCond:
    path: Path
    marker: str
    accept_existing: bool = False
    seen: set = field(default_factory=set)
    met: bool = False

    def describe(self) -> str:
        return f"MASTER line '- ... {self.marker}' in {self.path}"

    def lines(self) -> list[str]:
        try:
            return self.path.read_text("utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []

    def start(self) -> None:
        if not self.accept_existing:
            self.seen = {_digest(l) for l in self.lines() if marker_line(l, self.marker)}

    def poll(self) -> bool:
        for line in self.lines():
            if marker_line(line, self.marker) and _digest(line) not in self.seen:
                say(f"marker line found: {line[:160]}")
                return True
        return False


# --- systemd unit ----------------------------------------------------------------------------

def systemctl_show(unit: str, user: bool) -> dict[str, str]:
    cmd = ["systemctl", *(["--user"] if user else []), "show", unit, "--property",
           "LoadState,ActiveState,SubState,Result,InvocationID"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise Stop(3, f"systemctl show {unit} failed: {done.stderr.strip()[:200]}")
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


@dataclass
class UnitCond:
    unit: str
    state: str
    user: bool
    accept_existing: bool = False
    show: Callable[[str, bool], dict] = systemctl_show
    start_invocation: str = ""
    was_running: bool = False
    met: bool = False

    def describe(self) -> str:
        return f"unit {self.unit} {self.state}"

    def read(self) -> dict:
        props = self.show(self.unit, self.user)
        if props.get("LoadState") != "loaded":
            raise Stop(3, f"unit {self.unit} is {props.get('LoadState') or 'unknown'} "
                          "(systemd reports an unknown unit as inactive; refusing)")
        return props

    def start(self) -> None:
        props = self.read()
        self.start_invocation = props.get("InvocationID", "")
        self.was_running = props.get("ActiveState") in ("active", "activating", "reloading",
                                                        "deactivating")

    def poll(self) -> bool:
        p = self.read()
        active = p.get("ActiveState")
        if self.state == "active":
            return active == "active"
        ran = (self.accept_existing or self.was_running
               or p.get("InvocationID", "") != self.start_invocation)
        ended = active in ("inactive", "failed")
        if not (ran and ended):
            return False
        failed = active == "failed" or p.get("Result", "success") != "success"
        if self.state == "failed":
            return failed
        if self.state == "success" and failed:
            raise Stop(7, f"unit {self.unit} ended {active}/{p.get('Result')}, not success")
        return True


# --- file --------------------------------------------------------------------------------------

@dataclass
class FileCond:
    path: Path
    accept_existing: bool = False
    start_stamp: Optional[tuple] = None
    met: bool = False

    def describe(self) -> str:
        return f"file {self.path}"

    def stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def start(self) -> None:
        self.start_stamp = None if self.accept_existing else self.stamp()

    def poll(self) -> bool:
        s = self.stamp()
        return s is not None and s[2] > 0 and s != self.start_stamp


# --- pre-flight --------------------------------------------------------------------------------

PROBE = ("grep -E '^MemAvailable:' /proc/meminfo; df -Pk {disk} | tail -1; "
         "(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null "
         "|| echo none)")


def run_probe(host: Optional[str], disk: str, ssh_config: Optional[str]) -> str:
    script = PROBE.format(disk=shlex.quote(disk))
    if host in (None, "", "local", "localhost"):
        cmd = ["bash", "-c", script]
    else:
        cmd = ["ssh", *(["-F", ssh_config] if ssh_config else []), "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=15", host, script]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(6, f"host {host or 'local'} unreachable ({type(exc).__name__})")
    if done.returncode != 0 and "MemAvailable" not in done.stdout:
        raise Stop(6, f"host {host or 'local'} unreachable: {done.stderr.strip()[:200]}")
    return done.stdout


def parse_probe(out: str) -> dict[str, Optional[float]]:
    """GB figures: ram (MemAvailable), disk (avail on the path), gpu (min free over GPUs;
    MemAvailable on a unified-memory GPU that reports [N/A]; None with no GPU)."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    ram = disk = None
    gpu_vals: list[str] = []
    for line in lines:
        if line.startswith("MemAvailable:"):
            ram = int(line.split()[1]) / 1048576
        elif re.match(r"^\S+\s+\d+\s+\d+\s+\d+\s+\d+%", line):
            disk = int(line.split()[3]) / 1048576
        else:
            gpu_vals.append(line)
    if ram is None or disk is None:
        raise Stop(6, "pre-flight figures unreadable (MemAvailable or df missing)")
    gpu: Optional[float]
    if not gpu_vals or gpu_vals == ["none"]:
        gpu = None
    elif all(v.strip("[] ").upper() in ("N/A", "NOT SUPPORTED") for v in gpu_vals):
        gpu = ram   # unified memory (DGX Spark GB10): the GPU allocates from system RAM
    else:
        try:
            gpu = min(float(v) for v in gpu_vals) / 1024
        except ValueError:
            raise Stop(6, f"GPU memory unreadable: {gpu_vals[:2]}")
    return {"ram": ram, "disk": disk, "gpu": gpu}


def preflight_short(fig: dict, args: argparse.Namespace) -> list[str]:
    short = []
    for key, need in (("ram", args.min_ram_gb), ("disk", args.min_disk_gb),
                      ("gpu", args.min_gpu_free_gb)):
        if need is None:
            continue
        have = fig.get(key)
        if have is None:
            short.append(f"{key}: no GPU found, need {need} GB free")
        elif have < need:
            short.append(f"{key}: {have:.1f} GB free < {need} GB")
    return short


# --- main --------------------------------------------------------------------------------------

def duration(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (e.g. 90s, 20m, 6h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wait_then", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--master", type=Path)
    p.add_argument("--marker")
    p.add_argument("--unit", action="append", default=[])
    p.add_argument("--user-unit", action="append", default=[])
    p.add_argument("--file", action="append", default=[], type=Path)
    p.add_argument("--accept-existing", action="store_true")
    p.add_argument("--now", action="store_true", help="no condition: pre-flight, then run")
    p.add_argument("--host")
    p.add_argument("--ssh-config", default=DEFAULT_SSH_CONFIG if os.path.exists(
        DEFAULT_SSH_CONFIG) else None)
    p.add_argument("--min-ram-gb", type=float)
    p.add_argument("--min-disk-gb", type=float)
    p.add_argument("--disk-path", default="/")
    p.add_argument("--min-gpu-free-gb", type=float)
    p.add_argument("--timeout", type=duration, default=duration("12h"))
    p.add_argument("--interval", type=duration, default=duration("30s"))
    return p


def conditions(args: argparse.Namespace, show: Callable = systemctl_show) -> list[Any]:
    conds: list[Any] = []
    if (args.master is None) != (args.marker is None):
        raise Stop(3, "--master and --marker go together")
    if args.marker is not None:
        if not MARKER.match(args.marker):
            raise Stop(3, f"marker {args.marker!r} refused: use an exact token like "
                          "[[REBUILD-LIVE-DONE]] (capitals, digits, dashes), never a phrase")
        conds.append(MasterCond(args.master, args.marker, args.accept_existing))
    for spec, user in [(u, False) for u in args.unit] + [(u, True) for u in args.user_unit]:
        name, _, state = spec.partition(":")
        state = state or "success"
        if state not in UNIT_STATES:
            raise Stop(3, f"unit state {state!r} unknown (one of {', '.join(UNIT_STATES)})")
        conds.append(UnitCond(name, state, user, args.accept_existing, show=show))
    for path in args.file:
        conds.append(FileCond(path, args.accept_existing))
    if not conds and not args.now:
        raise Stop(3, "no condition given (--master/--marker, --unit, --file, or --now)")
    return conds


def run(argv: Sequence[str], *, show: Callable = systemctl_show, probe: Callable = run_probe,
        execvp: Callable = os.execvp, sleep: Callable = time.sleep,
        clock: Callable = time.monotonic) -> int:
    argv = list(argv)
    if "--" not in argv:
        raise Stop(3, "give the command after --")
    cut = argv.index("--")
    args, command = build_parser().parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        raise Stop(3, "no command after --")
    conds = conditions(args, show)
    for c in conds:
        c.start()
    say("waiting for " + ("; ".join(c.describe() for c in conds) or "nothing (--now)"))
    deadline = clock() + args.timeout
    while True:
        for c in conds:
            if not c.met and c.poll():
                c.met = True
                say(f"met: {c.describe()}")
        if all(c.met for c in conds):
            break
        if clock() >= deadline:
            raise Stop(5, "timeout; not met: " + "; ".join(
                c.describe() for c in conds if not c.met))
        sleep(args.interval)
    wanted = any(v is not None for v in (args.min_ram_gb, args.min_disk_gb,
                                         args.min_gpu_free_gb))
    while wanted:
        fig = parse_probe(probe(args.host, args.disk_path, args.ssh_config))
        short = preflight_short(fig, args)
        if not short:
            say(f"pre-flight ok on {args.host or 'local'}: " + ", ".join(
                f"{k} {v:.1f} GB" for k, v in fig.items() if v is not None))
            break
        if clock() >= deadline:
            raise Stop(4, f"pre-flight not met on {args.host or 'local'}: " + "; ".join(short))
        say(f"pre-flight short on {args.host or 'local'}: " + "; ".join(short))
        sleep(args.interval)
    say("running: " + " ".join(shlex.quote(c) for c in command))
    execvp(command[0], command)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Stop as stop:
        say(f"STOP ({stop.code}): {stop}")
        return stop.code


if __name__ == "__main__":
    raise SystemExit(main())
