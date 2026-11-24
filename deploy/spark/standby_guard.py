#!/usr/bin/env python3
"""Which Postgres a Spark run reads: the Spark's hot standby when it is close enough behind
the box, else the box (through the eval tunnels' balancer socket), said either way.

    standby_guard.py [--max-lag 600] [--standby DIR] [--box DIR]   -> prints PGHOST=<dir>

Exit 0: the standby (in recovery, replay within ``--max-lag`` seconds of the box, its slot
on the box not lost); 3: the box, with the reason on stderr. Lag: none when the standby has
replayed up to the box's current WAL position (a quiet box writes nothing, so the last
replayed commit's age alone would read as lag); else the age of its last replayed commit.
Any failure to read either side picks the box. Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
PG17 = HOME / "plx-ai/pg17/usr/lib/postgresql/17/bin/psql"
LIBS = HOME / "plx-ai/pg17/usr/lib/aarch64-linux-gnu"


CONFIG = HOME / "plx-ai/standby.json"
MARKER = "STANDBY_SETTINGS_ERROR"   # spark_run.sh's box side raises an ops alarm on a line starting so


class StandbySettingsError(ValueError):
    """The Spark's standby.json is unreadable, incomplete or names something unknown; the text
    names the file and why."""


def host_standby(config: Path | None = None) -> tuple[str, str]:
    """(socket folder, slot name) of THIS Spark's hot standby, from ``~/plx-ai/standby.json``
    ({"socket": "standby2-run", "slot": "plx_sb4ec_standby"}; a relative socket is under
    ~/plx-ai). No file: the original standby (``standby-run``, slot ``plx_s7d3d``). A file
    that cannot be read, or that names anything else, is an error: the caller reads the box."""
    config = config or CONFIG
    if not config.exists():
        return str(HOME / "plx-ai/standby-run"), "plx_s7d3d"
    try:
        cfg = json.loads(config.read_text())
    except (OSError, ValueError) as exc:
        raise StandbySettingsError(f"{config}: cannot be read as JSON ({exc})") from exc
    if not isinstance(cfg, dict):
        raise StandbySettingsError(f"{config}: must be a JSON object with socket and slot")
    if set(cfg) - {"socket", "slot"}:
        raise StandbySettingsError(f"{config}: unknown setting(s) {sorted(set(cfg) - {'socket', 'slot'})}; "
                                   "only socket and slot are settings")
    socket, slot = cfg.get("socket"), cfg.get("slot")
    if not isinstance(socket, str) or not socket or not isinstance(slot, str) \
            or not slot or not slot.replace("_", "").isalnum():
        raise StandbySettingsError(f"{config}: socket and slot must both be given (slot letters, digits, _)")
    return str(HOME / "plx-ai" / socket), slot


def ask(psql: str, host: str, user: str, sql: str) -> list[str]:
    env = dict(os.environ, LD_LIBRARY_PATH=str(LIBS), PGCONNECT_TIMEOUT="5")
    out = subprocess.run([psql, "-X", "-At", "-F", "|", "-h", host, "-U", user, "-d", "plx",
                          "-c", sql], capture_output=True, text=True, timeout=20, env=env, check=True)
    return out.stdout.strip().split("|")


def lsn(text: str) -> int:
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def choose(standby: list[str], box: list[str], max_lag: float) -> tuple[bool, str]:
    """(use the standby, why) from the standby's (in recovery, replay lsn, seconds since
    its last replayed commit) and the box's (current lsn, slot wal_status)."""
    recovery, replay, age = standby
    current, slot = box
    if recovery != "t":
        return False, "the standby is not in recovery (promoted or not a standby)"
    if slot in ("", "lost"):
        return False, f"the box's slot is {slot or 'missing'}: the standby must be copied again"
    behind = lsn(current) - lsn(replay)
    if behind <= 0:
        return True, "standby caught up"
    lag = float(age) if age else float("inf")
    if lag > max_lag:
        return False, f"standby {lag:.0f} s behind ({behind / 2**20:.0f} MB WAL), over {max_lag:.0f} s"
    return True, f"standby {lag:.0f} s behind ({behind / 2**20:.0f} MB WAL)"


def pick(max_lag: float = 600, standby: str | None = None,
         box: str = str(HOME / "plx-ai/run"), user: str = "plx_eval", slot: str | None = None,
         psql: str = str(PG17)) -> tuple[bool, str]:
    """(read the standby, why): ``choose`` over both sides; any unreadable side: the box.
    The standby's socket folder and slot default to this Spark's own (``host_standby``)."""
    try:
        if standby is None or slot is None:
            own_socket, own_slot = host_standby()
            standby, slot = standby or own_socket, slot or own_slot
        s = ask(psql, standby, user, "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn(), "
                "extract(epoch FROM clock_timestamp() - pg_last_xact_replay_timestamp())")
        b = ask(psql, box, user, "SELECT pg_current_wal_lsn(), coalesce((SELECT wal_status "
                f"FROM pg_replication_slots WHERE slot_name = '{slot}'), '')")
        return choose(s, b, max_lag)
    except StandbySettingsError as exc:
        return False, f"{MARKER} {exc}: reading the box"
    except Exception as exc:  # noqa: BLE001 - any unreadable side reads the box
        return False, f"could not read the standby or the box ({type(exc).__name__}: {str(exc)[:200]})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-lag", type=float, default=600)
    ap.add_argument("--standby", default=None, help="default: this Spark's (standby.json)")
    ap.add_argument("--box", default=str(HOME / "plx-ai/run"))
    ap.add_argument("--user", default="plx_eval")
    ap.add_argument("--slot", default=None, help="default: this Spark's (standby.json)")
    ap.add_argument("--psql", default=str(PG17))
    a = ap.parse_args(argv)
    use, why = pick(a.max_lag, a.standby, a.box, a.user, a.slot, a.psql)
    try:
        standby = a.standby or host_standby()[0]
    except StandbySettingsError as exc:
        print(f"{MARKER} {exc}", file=sys.stderr)
        use, standby = False, ""
    print(f"PGHOST={standby if use else a.box}")
    print(("standby: " if use else "box: ") + why, file=sys.stderr)
    return 0 if use else 3


if __name__ == "__main__":
    sys.exit(main())
