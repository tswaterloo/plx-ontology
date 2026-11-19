# Postgres roles (ARCH T7)

Who logs in as what, so no routine session is a superuser. Times are ET.

| Login | Cluster | What it may do | File |
|---|---|---|---|
| `plx_api` | served (5432), db `plx` | read ontology/search/plx_meta; write its query log and feedback; append to each person's memory log and keep their memory items (migration 0030, no DELETE) | `plx_api_role.sql` (SEC-S-13) |
| `plx_loader` | served (5432), db `plx` | runs as `plx_owner`: owns the tables and sequences of the data schemas (ontology, search, plx_meta, memory, delta_stage, steps_stage), creates tables there; `temp_file_limit` | `plx_loader_role.sql` |
| `plx_eval` | served and test | every eval and harness (`scripts/eval_guard.py` sets `PGUSER`): SELECT on the read schemas, its own temporary tables, every transaction read-only by default; no write to any shared table | `plx_eval_role.sql` (ops-hardening-1117) |
| `plx_test` | test (5433) | create and drop its own throwaway databases (CREATEDB only) | `plx_test_role.sql` |
| `<host user>` (superuser) | both | migrations, the ingest host, agents' ad hoc work, the role files themselves | - |

None of them has a SUPERUSER or CREATEROLE attribute or a superuser's membership. The
loader owns tables, which superuser sessions (migrations, the ingest host, agents, and the API
until it runs as `plx_api`) still read and write; a table owner who could define code (a
trigger, rule, policy, view, function, operator or type) could make such a session run it as
a superuser. So in `plx` code stays a superuser's: functions, views, types and the schemas
are not the loader's, and the event trigger `plx_no_code` refuses every code-defining
statement to a session whose user is no superuser (it also fires for `plx_api`); the event trigger `plx_no_code_expr` refuses a non-superuser's table or index statement that leaves a column default, generated column, CHECK or index expression calling any function but a short list of plain ones (`plx_admin.safe_expr`), since those run in the session that writes the row. The event
trigger `plx_data_owner` gives a table a superuser creates in a data schema later (a
migration) to `plx_owner`. Probed on a rehearsal database (11-13,
`operating/tests/test_pg_loader_role.py`): the loader creates, indexes, truncates, analyzes
and drops tables and sequences, sets `temp_file_limit`; it is refused ALTER ROLE, CREATE ROLE,
`session_replication_role`, COPY ... PROGRAM, `pg_read_file`, SET ROLE, ALTER SYSTEM, DROP
EXTENSION, the event triggers and their functions, CREATE/replace FUNCTION (a trigger
function too, pg_temp too), CREATE TRIGGER, RULE, VIEW, POLICY, TYPE, OPERATOR and SCHEMA,
and ALTER DEFAULT PRIVILEGES.
Residual: as a table owner the loader can still disable or drop an existing trigger on its
tables (`memory.log_append_only`'s append-only guard), and change or delete the data, which
is a loader's job; neither runs anything as a superuser. The live database does not have
`plx_no_code_expr` yet: it comes with the next run of the file (the quiet-window step below),
before any unit logs in as the loader.

## Logging in as a role: peer maps
Each OS user maps to the role in `pg_ident.conf`, and a `pg_hba.conf` line above the generic
`local all all peer` uses the map:
```
# served cluster /etc/postgresql/17/main
pg_ident.conf:  plxeval    <host user>         plx_eval
                plxeval    plx-sparkpg    plx_eval
                plxapi     plx-workbench  plx_api
                plxapi     <host user>         plx_api
                plxloader  <host user>         plx_loader
pg_hba.conf:    local  plx  plx_eval    peer map=plxeval
                local  plx  plx_api     peer map=plxapi
                local  plx  plx_loader  peer map=plxloader
# test cluster /etc/postgresql/17/test
pg_ident.conf:  plxeval    <host user>  plx_eval
                plxtest    <host user>  plx_test
                plxloader  <host user>  plx_loader      (rehearsals only)
pg_hba.conf:    local  all  plx_eval    peer map=plxeval
                local  all  plx_test    peer map=plxtest
                local  all  plx_loader  peer map=plxloader
```
`plx-sparkpg` is the Spark runs' relay (`deploy/spark/systemd/plx-spark-pg-relay.*`, reached
by the 16 tunnels per Spark `plx-spark-pg-tunnel@<spark>:<n>` and spread over them on the Spark
by `deploy/spark/spark_pg_balance.py`, installed with `deploy/spark/spark_pg_install.sh`; a
system user with no login): the Sparks reach the served cluster only through it, and its one
mapping is `plx_eval`, so a Spark run is a full reader that can only read (owner-level evals);
it is refused every other role (`plx_api`, `plx_loader`, `postgres`: no map, and the generic
`peer` line needs the OS user's own name). An eval as a staff or restricted reader stays on
the box until a scoped Spark role exists. Undo: drop the line, `SELECT pg_reload_conf()`.
Backups of the files before each change: `*.bak-s13` (API), `*.bak-t7` (loader, tests), `*.bak-eval` (eval role, 11-17), `pg_ident.conf.bak-sparkpg` (Spark relay).
`sudo pg_ctlcluster 17 <main|test> reload` applies them; nothing restarts.

## Tests
`tools/testslot.py` sets `PGUSER=plx_test` for a run it points at the test cluster, when that
login works and is no superuser (else the run logs in as before). The few tests that prove a
superuser-run deploy file (the role files) log in as the OS user's own superuser explicitly
(`operating.pg.scratch_db.superuser_dsn`). Undo: delete the two map lines (or set
`PLX_TEST_PG_USER` to the OS user), then `DROP ROLE plx_test` once its databases are gone.

## Loaders
1. `psql -X -d plx -f deploy/pg/plx_loader_role.sql`; re-run until `left_with_another_owner`
   is 0 (a table a long reader holds misses its 3 s lock wait and is moved on a later run).
2. A loader unit logs in as the role through `PGUSER` (the DSN names no user, so libpq takes
   it; the env files are left alone). A drop-in per unit, used on the unit's next start (the
   path/timer units start on their own; nothing is restarted by hand):
   ```
   # /etc/systemd/system/<unit>.service.d/zz-pg-loader.conf
   [Service]
   Environment=PGUSER=plx_loader
   ```
   Units: `plx-ontology-pg-follow`, `plx-search-follow`, `plx-search-generation`,
   `plx-delta-compact`, `plx-delta`. Then `sudo systemctl daemon-reload`, and after each
   unit's next run: `journalctl -u <unit> --since -15min | grep -i 'permission denied\|must be owner'`.
3. Undo a unit: delete its drop-in and daemon-reload (its next run logs in as before).
   Undo everything: the drop-ins, then `psql -X -d plx -f deploy/pg/plx_loader_role_undo.sql`.

## plx-ingest (an operator step, never an automated one)
`plx-ingest` (operating.host serve, ingest mode) is the capture host and stays on the owner
login until an operator moves it in a quiet window: add the same drop-in to
`plx-ingest.service`, `daemon-reload`, and restart it only when no capture cycle is mid-run
(`python3 -m operating.host status --state-dir $PLX_INGEST_STATE` shows idle). Its Postgres
work is the same delta/follow statements as the loaders above, so the grants cover it; watch
the first cycle's journal for `permission denied`, and undo by deleting the drop-in and
restarting it again. The job queue (`plx_jobq` database, `plx-jobq-worker`) and the
proactive, truth-check and autopilot units are readers plus their own state; they move to a
reader role in a later step (the API's grants are the model).

## Live state and the read check (11-13, 8:45 PM ET)
The first version of the role file (8:00 PM) moved two views (`ontology.object_values`,
`object_values_typed`) to `plx_owner` while 9 of their tables stayed the superuser's (their
long readers held the lock), so every read through the views failed until the coordinator
granted plx_owner SELECT. Fixed: the views are the superuser's again (checked first in a
rolled-back transaction, then committed), and the file now grants plx_owner SELECT on
every relation of the data schemas and never moves a view. Before and after any further
ownership step, run as the superuser, as plx_api and as plx_owner:
```sh
for r in "" "SET ROLE plx_api;" "SET ROLE plx_owner;"; do
  psql -X -At -d plx -c "$r" -f deploy/pg/read_check.sql | grep -ci error; done
```
(expected 0 / 7 / 0 once migration 0030 is applied: plx_api has never been granted the
delta_stage schema; 0 / 9 / 0 before it, when memory.log and memory.item fail too.)
Still to do, in a quiet window only (no delta, follower or proactive run reading the
ontology), with the read check after: the 9 ontology tables still the superuser's
(objects, object_versions, value_sources, link_rows, link_skeletons, link_texts,
schema_migrations, load_state, store_meta) - re-run the role file. Only then add the unit
drop-ins. One-command undo of everything: `psql -X -d plx -f deploy/pg/plx_loader_role_undo.sql`.
