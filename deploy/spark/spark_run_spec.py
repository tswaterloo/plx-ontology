"""spark_run.sh's run description: one JSON line for spark_run_remote.py (read on its stdin,
so no variable's value is ever on a command line or in a file on the Spark).

usage: spark_run_spec.py <n ship> <ship>... <n out> <out>... <n env> <name>... <command> [args...]
       (SPEC_SHA, SPEC_TREE, SPEC_CWD, SPEC_STATE, SPEC_NAME in the environment)

Variables passed: TZ/LANG/LC_ALL and every PLX_* whose name does not look like a secret,
plus each name given (``--env``), secret or not; nothing else. A name that is not a
variable name refuses (exit 2).

The people exclusion goes with every run (``exclusion_env``): a run on the Spark reads the
Spark's state copy, which holds no exclusion (its env file lives in the box user's config,
never synced), so a replay or eval there refused every person's mail and chats (fail closed).
When the caller names no list itself, the box's current list (``environ_with_exclusion``: the
passes' env file) is read and passed as ``PLX_EXCLUDED_PEOPLE_OIDS``, like any other variable
on ssh's stdin (never a command line, never a file on the Spark); a box path in
``PLX_EXCLUDED_PEOPLE_FILE`` is replaced by the ids it names. A list the guard cannot use
(absent, unreadable, invalid) passes nothing, so the run still fails closed there.
Visibility on the Spark: the ids live only in the run's process environment (never a file,
a log or a command line), so the Spark's own login (the same uid) and root can read them
from /proc/<pid>/environ while the run lives; no other user can."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable, Mapping, Sequence

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


def exclusion_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``{"PLX_EXCLUDED_PEOPLE_OIDS": ids}`` for the box's usable exclusion (module
    docstring), else {}."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from integrations.people_exclusion import ENV_IDS, PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(environ))
    # its parsed ids (the guard's own list; integrations/ is step code, so no new accessor)
    ids = sorted(getattr(guard, "_ids", ()) or ()) if guard.unavailable is None else []
    return {ENV_IDS: ",".join(ids)} if guard.unavailable is None and ids else {}


def build(argv: Sequence[str], environ: Mapping[str, str], *,
          exclusion: Callable[[Mapping[str, str]], dict[str, str]] = exclusion_env) -> dict:
    a = list(argv)
    n = int(a[0]); ship = a[1:1 + n]; a = a[1 + n:]
    o = int(a[0]); out = a[1:1 + o]; a = a[1 + o:]
    m = int(a[0]); named = a[1:1 + m]; cmd = a[1 + m:]
    if not cmd:
        raise ValueError("no command")
    env = passed_env(environ, named)
    env.pop("PLX_EXCLUDED_PEOPLE_FILE", None)      # a box path: its ids go instead
    env.pop("PLX_EXCLUDED_PEOPLE_OIDS", None)
    env.update(exclusion(environ))
    return {"sha": environ["SPEC_SHA"], "tree": environ["SPEC_TREE"],
            "cwd": environ["SPEC_CWD"], "state": environ["SPEC_STATE"],
            "pg": environ.get("SPEC_PG", "box"),
            "name": environ["SPEC_NAME"], "ship": ship, "out": out, "env": env,
            "cmd": cmd}


if __name__ == "__main__":
    try:
        print(json.dumps(build(sys.argv[1:], os.environ)))
    except (ValueError, IndexError, KeyError) as exc:
        print(f"spark_run: {exc}", file=sys.stderr)
        sys.exit(2)
