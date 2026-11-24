"""ship_swap.py: move a Spark-built comms table onto the box and swap it in (master plan
specs M4 steps 7 and 9). Run on the box from the deployed release (PYTHONPATH = the release).

  python3 deploy/spark/train/ship_swap.py ship TRAIN VIEW EXPECTED_PRINT
      stream the train copy's biz.VIEW (s7d3d, ~/train-TRAIN, port 55721) into biz.VIEW_ship,
      its indexes, ANALYZE; the copy's state row kept beside it in
      <PLX_STATE_DIR>/trains/TRAIN/ship-state-VIEW.json. Refused unless the copy was built by
      EXPECTED_PRINT and the disk budget (operating.disk_budget) grants the table's size x 1.2;
      paused while the standby lags over LAG_PAUSE_GB (resumes under LAG_RESUME_GB). Any
      failure drops VIEW_ship; the served table is never touched.
  python3 deploy/spark/train/ship_swap.py swap TRAIN VIEW
      refused unless the deployed code's print of VIEW equals the shipped copy's; then, under
      the refresh's advisory lock (waits for a running refresh), the state row, the table and
      its guarded view are swapped in one transaction and the old table dropped.

Secrets are unset by the caller's environment (this reads nothing but the two databases)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOST = os.environ.get("PLX_TRAIN_HOST", "s7d3d")
SSH = ["ssh", "-F", "/data/plx-state/ts-petra/ssh_config", "-o", "BatchMode=yes", HOST]
PORT = 55721
SHIP_FACTOR = 1.2       # the shipped table plus its indexes, against the copy's table size
LAG_PAUSE_GB, LAG_RESUME_GB = 8.0, 2.0   # the WAL budget's standby lag lines


def _psql_remote(train: str) -> str:
    return ("LD_LIBRARY_PATH=$HOME/plx-ai/pg17/usr/lib/aarch64-linux-gnu $HOME/plx-ai/pg17/usr/"
            f"lib/postgresql/17/bin/psql -X -h $HOME/train-{train}/run -p {PORT} -d plx -At "
            "-v ON_ERROR_STOP=1")


def copy_sql(train: str, sql: str) -> str:
    return subprocess.run(SSH + [_psql_remote(train) + " -c " + json.dumps(sql)],
                          capture_output=True, text=True, check=True).stdout


def state_file(train: str, view: str) -> Path:
    d = Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state") / "trains" / train
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d / f"ship-state-{view}.json"


def lag_gb(box) -> float:
    got = box.execute("SELECT coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)), 0) "
                      "FROM pg_stat_replication").fetchone()[0]
    return float(got) / 1024 ** 3


def ship(train: str, name: str, expected: str) -> int:
    import psycopg
    from operating import disk_budget
    from operating.pg import business_views as bv, business_comms as bc
    v, S, out = bv.VIEWS[name], bv.SCHEMA, f"{name}_ship"
    state = json.loads(copy_sql(train, f"select row_to_json(s) from {S}.state s where view='{name}'"))
    if state["notes"].get("code") != expected:
        print(f"refused: {name} was built by {state['notes'].get('code')}, not {expected}")
        return 2
    size = int(copy_sql(train, f"select pg_total_relation_size('{S}.{name}')").strip())
    disk_budget.reserve(f"ship:{name}", size * SHIP_FACTOR / 1024 ** 3, ttl_s=6 * 3600)
    t0 = time.time()
    try:
        cols = bc.columns(v)
        names = ", ".join(f'"{n}"' for n, _ in cols)
        with psycopg.connect("dbname=plx", autocommit=True) as box:
            while lag_gb(box) > LAG_PAUSE_GB:
                print(json.dumps({"paused": f"standby lag {lag_gb(box):.1f} GB"}), flush=True)
                while lag_gb(box) > LAG_RESUME_GB:
                    time.sleep(30)
            box.execute(f"DROP TABLE IF EXISTS {S}.{out}")
            box.execute(f"CREATE TABLE {S}.{out} (" + ", ".join(f'"{n}" {t}' for n, t in cols)
                        + ")")
        src = subprocess.Popen(SSH + [_psql_remote(train) + " -c " + json.dumps(
            f"COPY (SELECT {names} FROM {S}.{name}) TO STDOUT (FORMAT binary)")],
            stdout=subprocess.PIPE)
        dst = subprocess.run(["psql", "-X", "-d", "plx", "-v", "ON_ERROR_STOP=1", "-c",
                              f"COPY {S}.{out} ({names}) FROM STDIN (FORMAT binary)"],
                             stdin=src.stdout, capture_output=True, text=True)
        src.wait()
        if src.returncode or dst.returncode:
            raise RuntimeError(f"ship of {name} failed: {dst.stderr}")
        t1 = time.time()
        idx = [line for line in copy_sql(train, "select indexdef from pg_indexes where "
                                         f"schemaname='{S}' and tablename='{name}'").splitlines()
               if line]
        with psycopg.connect("dbname=plx", autocommit=True) as box:
            n_copy = int(copy_sql(train, f"select count(*) from {S}.{name}").strip())
            n_ship = box.execute(f"SELECT count(*) FROM {S}.{out}").fetchone()[0]
            if n_copy != n_ship:
                raise RuntimeError(f"{name}: {n_ship} rows shipped of {n_copy}")
            box.execute("SET maintenance_work_mem = '2GB'")
            for d in idx:
                on = d.split(" ON ", 1)[1].replace(f"{S}.{name} ", f"{S}.{out} ", 1)
                box.execute(("CREATE UNIQUE INDEX ON " if d.startswith("CREATE UNIQUE")
                             else "CREATE INDEX ON ") + on)
            box.execute(f"ANALYZE {S}.{out}")
        state_file(train, name).write_text(json.dumps(state))
        print(json.dumps({"view": name, "shipped": n_ship, "bytes": size,
                          "ship_s": round(t1 - t0), "index_s": round(time.time() - t1),
                          "indexes": len(idx)}), flush=True)
        return 0
    except BaseException:
        with psycopg.connect("dbname=plx", autocommit=True) as box:
            box.execute(f"DROP TABLE IF EXISTS {S}.{out}")
        raise
    finally:
        disk_budget.release(f"ship:{name}")


def swap(train: str, name: str) -> int:
    import psycopg
    from operating.pg import business_views as bv, swap_lock
    from operating.pg import business_views_code as code
    v, S, out = bv.VIEWS[name], bv.SCHEMA, f"{name}_ship"
    state = json.loads(state_file(train, name).read_text())
    if state["notes"].get("code") != code.fingerprint(v):
        print(f"refused: the deployed print {code.fingerprint(v)} is not the copy's "
              f"{state['notes'].get('code')}")
        return 2
    with psycopg.connect("dbname=plx", autocommit=True) as box:
        if box.execute("SELECT to_regclass(%s)", (f"{S}.{out}",)).fetchone()[0] is None:
            print(f"refused: no {S}.{out}")
            return 2
        box.execute("SET lock_timeout = '5s'")
        while not box.execute("SELECT pg_try_advisory_lock(%s)", (bv.REFRESH_LOCK,)).fetchone()[0]:
            time.sleep(10)   # a refresh is running: wait for it to end
        try:
            guarded = bv._guarded_views()
            with box.transaction():
                box.execute(
                    f"INSERT INTO {S}.state (view, built_at, digest, rows, seconds, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (view) DO UPDATE SET "
                    "built_at = excluded.built_at, digest = excluded.digest, rows = "
                    "excluded.rows, seconds = excluded.seconds, notes = excluded.notes",
                    (name, state["built_at"], state["digest"], state["rows"], state["seconds"],
                     json.dumps(state["notes"])))

                def do() -> None:
                    if guarded is not None:
                        guarded.drop(box, name, S)
                    box.execute(f"DROP TABLE IF EXISTS {S}.{name}_old")
                    box.execute(f"ALTER TABLE IF EXISTS {S}.{name} RENAME TO {name}_old")
                    box.execute(f"ALTER TABLE {S}.{out} RENAME TO {name}")
                    box.execute(f"DROP TABLE IF EXISTS {S}.{name}_old")
                    if guarded is not None:
                        guarded.create(box, v, S)
                    box.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                                f"'plx_api') THEN EXECUTE 'GRANT SELECT ON {S}.{name} TO plx_api'; "
                                "END IF; END $$")
                t = time.time()
                swap_lock.swap_in(box, do)
        finally:
            box.execute("SELECT pg_advisory_unlock(%s)", (bv.REFRESH_LOCK,))
    print(json.dumps({"view": name, "swapped": True, "swap_s": round(time.time() - t, 3),
                      "code": state["notes"].get("code")}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "ship":
        return ship(argv[1], argv[2], argv[3])
    if len(argv) == 3 and argv[0] == "swap":
        return swap(argv[1], argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
