#!/usr/bin/env python3
"""Box side of a Spark's broken standby.json: after a spark_run, raise one ops alarm for it.

    standby_settings_alarm.py <spark> <stderr-file> [--asked]

The Spark's guard prints a line containing ``STANDBY_SETTINGS_ERROR <file>: <reason>`` when
its standby.json is unreadable, incomplete or names an unknown setting (and reads the box).
This finds that line (spark_run's own, not a job's output) in the run's stderr and calls the host's alarm
(``unit_alarm.py raise standby-settings:<spark> <message>``, in the ops attention list) once
per change of the message: the last message sent is kept per Spark, so the same breakage on
every run alarms once and a different one alarms again. ``--asked`` (the run asked for the
standby) with no such line means the file is fine now: the kept message is cleared so the same
breakage later alarms again. Exit 0 always except an alarm that could not be raised (4): the
run itself already read the box and said why.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MARKER = "STANDBY_SETTINGS_ERROR"
ALARM = os.environ.get("PLX_UNIT_ALARM", "/usr/local/lib/plx-ops/unit_alarm.py")
STATE = Path(os.environ.get("PLX_STANDBY_ALARM_STATE",
                            str(Path.home() / ".local/state/plx-standby-settings-alarm")))


PREFIX = "spark_run: postgres = the box: " + MARKER + " "   # the line spark_run_remote.database prints


def message(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(PREFIX):
            return line[len(PREFIX):].strip()
    return None


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2 or not args[0].replace("-", "").isalnum():
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    spark, errfile = args
    asked = "--asked" in argv
    msg = message(Path(errfile).read_text(errors="replace")) if Path(errfile).exists() else None
    seen = STATE / spark
    if msg is None:
        if asked:
            seen.unlink(missing_ok=True)
        return 0
    if seen.exists() and seen.read_text() == msg:
        return 0
    text = f"{spark}: the standby settings file is broken, runs read the box instead: {msg}"
    done = subprocess.run([sys.executable, ALARM, "raise", f"standby-settings:{spark}", text],
                          capture_output=True, text=True)
    if done.returncode != 0:
        print(f"standby_settings_alarm: could not raise the alarm: {done.stderr.strip()[:200]}", file=sys.stderr)
        return 4
    STATE.mkdir(parents=True, exist_ok=True)
    seen.write_text(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
