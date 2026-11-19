"""spark_run.sh's run description: one JSON line for spark_run_remote.py (read on its stdin,
so no variable's value is ever on a command line or in a file on the Spark).

usage: spark_run_spec.py <n ship> <ship>... <n out> <out>... <n env> <name>... <command> [args...]
       (SPEC_SHA, SPEC_TREE, SPEC_CWD, SPEC_STATE, SPEC_NAME in the environment)

Variables passed: TZ/LANG/LC_ALL and every PLX_* whose name does not look like a secret,
plus each name given (``--env``), secret or not; nothing else. A name that is not a
variable name refuses (exit 2)."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Mapping, Sequence

SECRET = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN|COOKIE|AUTH|TICKET", re.I)
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def passed_env(environ: Mapping[str, str], named: Sequence[str]) -> dict[str, str]:
    for k in named:
        if not NAME.match(k):
            raise ValueError(f"--env {k!r} is not a variable name")
    env = {k: v for k, v in environ.items()
           if (k in ("TZ", "LANG", "LC_ALL") or k.startswith("PLX_")) and not SECRET.search(k)
           and not k.startswith("PLX_SPARK_")}
    env.update({k: environ[k] for k in named if k in environ})
    return env


def build(argv: Sequence[str], environ: Mapping[str, str]) -> dict:
    a = list(argv)
    n = int(a[0]); ship = a[1:1 + n]; a = a[1 + n:]
    o = int(a[0]); out = a[1:1 + o]; a = a[1 + o:]
    m = int(a[0]); named = a[1:1 + m]; cmd = a[1 + m:]
    if not cmd:
        raise ValueError("no command")
    return {"sha": environ["SPEC_SHA"], "tree": environ["SPEC_TREE"],
            "cwd": environ["SPEC_CWD"], "state": environ["SPEC_STATE"],
            "name": environ["SPEC_NAME"], "ship": ship, "out": out, "env": passed_env(environ, named),
            "cmd": cmd}


if __name__ == "__main__":
    try:
        print(json.dumps(build(sys.argv[1:], os.environ)))
    except (ValueError, IndexError, KeyError) as exc:
        print(f"spark_run: {exc}", file=sys.stderr)
        sys.exit(2)
