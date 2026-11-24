"""The people-set leak scan (The two excluded people's data
is kept and shown to the owner alone): counts what of the people set
(``ontology.people_excluded_objects``) a reader other than the owner can reach, so a leak
raises an alarm (``ops_alarms`` check ``exclusion_leak``, critical) at once.

    python3 -m operating.exclusion_leak run --state-dir STATE [--dsn DSN]

Run hourly (``plx-exclusion-leak.timer``). Read-only. It counts:

* **business rows not the owner's**: rows of every DOOR v2 schema whose lineage names an
  object of the set (its own or a read record) while their access class does not hold
  ``access_classes.OWNER_KEY`` (``door.exclude`` moves them; a row it missed is a leak);
* **communications rows not flagged**: rows of the communications tables
  (``business_comms``) of an object of the set whose ``excluded`` flag reads false
  (every reader but the owner reads exactly the rows flagged false);
* **an excluded person's own items outside the set**: served versions of their mailbox
  (``graph-mail://<id>/``) or a Teams chat whose id holds their object id, at or below the
  set's ``covered_ver`` (decided), that the set does not hold.

No set state, or one of another exclusion list, is the finding itself (fails closed). The
excluded ids come from the environment (``PeopleExclusion.from_env``); an unavailable list
fails closed. Writes ``STATE/exclusion-leak/status.json`` (0600): counts per kind and
when; never an id, address or handle. Exit 0 after a scan whatever it found, 1 when it
could not scan (the status records why), 2 on an unreadable state dir.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import time
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA = "plx-exclusion-leak/v1"
HOME, STATUS = "exclusion-leak", "status.json"
# each statement's bound, on both connections (the counts and the purge's dry run)
STATEMENT_TIMEOUT_MS = 600_000


def _prefix_range(prefix: str) -> tuple[str, str]:
    """[low, high) of every text starting with ``prefix`` that ends in ``/`` under the
    database's byte order (``C.UTF-8``; ``/`` + 1 is ``0``)."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _tables(cur: Any, schema: str) -> list[str]:
    cur.execute("SELECT table_name FROM information_schema.columns WHERE table_schema = %s "
                "AND column_name IN ('access_class', 'handle') GROUP BY 1 "
                "HAVING count(*) = 2 ORDER BY 1", (schema,))
    # a refresh's build and swap tables (<view>_next, _new, _ship, _old) serve no reader and
    # come and go while a refresh runs: scanning one that was just swapped away failed the
    # whole scan (UndefinedTable) on most runs of 11-23
    return [r[0] for r in cur.fetchall() if not r[0].endswith(STAGING_SUFFIXES)]


STAGING_SUFFIXES = ("_next", "_new", "_ship", "_old")


def business_unmarked(cur: Any) -> int:
    """Rows of the DOOR v2 schemas built from the set whose class is not the owner's."""
    from operating.pg import door
    from operating.pg.access_classes import OWNER_KEY
    total = 0
    for schema in door.schemas(cur.connection if hasattr(cur, "connection") else cur):
        for view in _tables(cur, schema):
            cur.execute(
                f"SELECT count(*) FROM {schema}.{view} t JOIN {schema}.lineage l "
                "ON l.view = %s AND l.handle = t.handle JOIN "
                f"{schema}.access_classes a ON a.id = t.access_class "
                "WHERE NOT (%s = ANY(a.audience_keys)) AND EXISTS (SELECT 1 FROM "
                "unnest(l.object_ids || l.read_ids) o JOIN ontology.people_excluded_objects p "
                "ON p.object_id = o)", (view, OWNER_KEY))
            total += int(cur.fetchone()[0])
    return total


def comms_unflagged(cur: Any) -> int:
    """Rows of the communications tables of an object of the set flagged as not in it."""
    from operating.pg import business_comms as bc
    from operating.pg import business_views as bv
    total = 0
    for view in sorted(bc.SPECS):
        cur.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.{view}",))
        if cur.fetchone()[0] is None:
            continue
        cur.execute(f"SELECT count(*) FROM {bv.SCHEMA}.{view} t JOIN "
                    "ontology.people_excluded_objects p ON p.object_id = t.object_ids[1] "
                    "WHERE t.excluded IS FALSE")
        total += int(cur.fetchone()[0])
    return total


def own_outside_set(cur: Any, ids: Sequence[str], covered: int) -> dict[str, int]:
    """Decided versions of an excluded person's mailbox or 1:1 chats the set lacks."""
    out = {"mailbox_versions": 0, "chat_versions": 0}
    missing = ("AND v.ver <= %s AND NOT EXISTS (SELECT 1 FROM "
               "ontology.people_excluded_objects p WHERE p.l1_handle = v.l1_handle)")
    for oid in ids:
        low, high = _prefix_range(f"graph-mail://{oid}/")
        cur.execute("SELECT count(*) FROM ontology.object_versions v WHERE "
                    "v.l1_handle >= %s AND v.l1_handle < %s " + missing, (low, high, covered))
        out["mailbox_versions"] += int(cur.fetchone()[0])
    if ids:
        cur.execute("SELECT count(*) FROM ontology.object_versions v WHERE v.l1_handle >= "
                    "'teams-chat://' AND v.l1_handle < 'teams-chat:/0' AND v.l1_handle ~* %s "
                    + missing, ("(" + "|".join(sorted(ids)) + ")", covered))
        out["chat_versions"] = int(cur.fetchone()[0])
    return out


def scan(state: Path, dsn: str, environ: Mapping[str, str], *,
         connect: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    """Counts only (module docstring): ``served`` sums what a reader other than the owner
    can reach of the set."""
    from integrations.people_exclusion import PeopleExclusion
    from operating.pg import people_excluded as pe
    guard = PeopleExclusion.from_env(environ)
    if guard.unavailable:
        return {"ok": False, "error": f"exclusion unavailable ({guard.unavailable})"}
    # The excluded ids, read here rather than through a new PeopleExclusion method:
    # integrations/people_exclusion.py is step code (its bytes feed the steps digest).
    guard.require()
    ids = sorted(frozenset(getattr(guard, "_ids")))
    tag = pe.current_exclusion(environ)
    if connect is None:
        import psycopg

        def connect(target: str) -> Any:
            conn = psycopg.connect(target, connect_timeout=10, autocommit=True,
                                   application_name="plx-exclusion-leak",
                                   options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                                           "-c default_transaction_read_only=on")
            return conn
    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT exclusion, covered_ver FROM {pe.STATE}")
            state_row = cur.fetchone()
            if state_row is None or state_row[0] != tag or state_row[1] is None:
                return {"ok": False, "error": "people set not built for this list"}
            served = {"business_rows_not_owners": business_unmarked(cur),
                      "comms_rows_unflagged": comms_unflagged(cur),
                      **own_outside_set(cur, ids, int(state_row[1]))}
    finally:
        conn.close()
    return {"ok": True, "excluded_people": len(ids), "served": served,
            "served_total": sum(served.values())}


SWAP_RETRIES = 1                 # one rerun when a refresh swaps a view mid-scan
SWAP_WAIT_S = 45.0
TRANSIENT = ("UndefinedTable", "UndefinedColumn")


def run(state: Path, dsn: str, environ: Mapping[str, str] = os.environ, *,
        now: Optional[datetime] = None, scanner: Callable[..., dict[str, Any]] = scan,
        sleep: Callable[[float], Any] = time.sleep) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    home = state / HOME
    home.mkdir(mode=0o700, exist_ok=True)
    found: dict[str, Any] = {}
    for attempt in range(1 + SWAP_RETRIES):
        try:
            found = scanner(state, dsn, environ)
            break
        except Exception as exc:  # noqa: BLE001 - a scan that cannot run is the finding
            found = {"ok": False, "error": f"scan failed ({type(exc).__name__})"}
            # a business view swapped away mid-scan (UndefinedTable / a table or column gone):
            # transient by construction; one rerun after the swap, a real fault fails again
            if type(exc).__name__ not in TRANSIENT or attempt >= SWAP_RETRIES:
                break
            sleep(SWAP_WAIT_S)
    status = {"schema": SCHEMA, "checked_at": now.isoformat().replace("+00:00", "Z"),
              **found}
    tmp = home / (STATUS + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(status, f, indent=1)
    os.replace(tmp, home / STATUS)
    return status


def window_skip(state: Path) -> Optional[dict[str, Any]]:
    """The window lock's skip report while a window blocks ``exclusion-leak`` (the scan
    then waits for the next hour; a status older than 3 h raises its own alarm), else None.
    The guard keeps the job's running lock for the process, so a window taken later waits
    for this scan. A broken lock never stops the scan (logged)."""
    try:
        from ontology import window_lock
        return window_lock.skip_report("exclusion-leak", state_dir=state)
    except Exception as exc:  # noqa: BLE001 - logged, the scan runs
        print(f"window check failed, running anyway: {exc}", file=sys.stderr)
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state-dir", required=True, type=Path)
    r.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN") or "postgresql:///plx")
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    held = window_skip(args.state_dir)
    if held:   # a migration or maintenance window: its DDL never waits behind this scan
        print(json.dumps(held, sort_keys=True))
        return 0
    status = run(args.state_dir, args.dsn)
    print(json.dumps({k: status.get(k) for k in ("checked_at", "ok", "served_total",
                                                  "error")}))
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
