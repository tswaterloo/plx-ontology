# One release for every unit

Before this, the API, app, delta, proactive engine and truth check ran from
`/data/scratch/wt/deploy` (main plus a hand-made schema-revert commit). Ingest ran from the dev
checkout and the search units from `/data/scratch/wt/search`. Each was a different commit, and
all of them wrote to or read the same stores. Now every unit runs from **one release**:

- `deploy/release/plx-release build <ref>` unpacks one commit (git archive: nothing is
  checked out) into `$PLX_RELEASES/<sha12>` (`/data/plx-releases`). With `--overlay
  <commit>:<path>` it takes files held back from another commit, such as the served
  generation's schema; the release id then gets a suffix. It refuses a release whose schema
  digest differs from the served projection's. It builds the app inside the release, with
  `node_modules` hard-linked from the current release when `package-lock.json` is unchanged.
- Every unit has `WorkingDirectory=$PLX_RELEASES/current` (`current/app` for the app,
  `current/deploy/Caddyfile` for Caddy) and the one venv `/data/venv`.
- `plx-release switch <id>` points `current` at the release and `previous` at the old one.
  Each link is replaced in one rename, so the switch is atomic. `plx-release restart`
  restarts the long-running units that are active. A serving API is never restarted cold:
  the standby plx-api@standby starts on the new release on its own unix socket and opens it
  only once warm, plx-api.service restarts held the same way while Caddy sends readers to
  the standby, and the standby stops once 8780 serves again; Caddy is not reloaded for it
  (`deploy/release/README-warm-switch.md`). Without that host install, or with no memory or
  Postgres room for a second API, the API restarts in place and the reason is printed.
  A deploy stops each API gracefully: requests under way get 60 s to end, so an Ask answer
  still streaming after 60 s is cut (the reader asks again).
  Timer-driven units (delta, proactive, truth check, AI watcher, backup) load the new
  release on their next run. A running process keeps the release it started with, and
  `prune` never removes a release a process still runs from.
- `plx-release rollback && plx-release restart` goes back to the previous release.
- The ops scripts are in `deploy/ops/` (moved from `/data/scratch/cfg`) and the host drop-ins
  in `deploy/systemd/host/<unit>.d/`. The units are templates (`@PLX_USER@`, `@PLX_HOME@`,
  `@PLX_CURRENT@`, `@PLX_HOSTNAME@`). The scripts read their paths from `deploy/lib.sh`,
  and the real values come from `~/.config/plx/deploy.env` on the host. No username or
  home path is committed (`tests/test_deploy_release.py`).
  `rishi_scrub.py` and its inventories stay host-only in `$PLX_OPS_PRIVATE`
  (`/data/scratch/cfg`), because their rule lists name people. The spent one-off queues
  (`land_queue2..25.sh`, dated `land_*` scripts, `*.bak`) and the dev-checkout loops
  (`cycles_loop.sh`, `cold_cycle.sh`, `teams_loop.sh`, `sp_named.sh`, `originals.sh`,
  `retr_gated.sh`, `cutover_ingest.sh`, `install_search.sh`) were not moved.
  `land_queue.sh` now reads its entries from a file.

## Switch-over (once)

Run these as the host user. All times are ET.

```sh
# 0. Host settings (never committed). PLX_HOSTNAME is the tailnet name.
cat > ~/.config/plx/deploy.env <<EOF
PLX_USER=$(id -un)
PLX_GROUP=$(id -gn)
PLX_HOME=$HOME
PLX_HOSTNAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
EOF
chmod 600 ~/.config/plx/deploy.env
sudo install -d -o $(id -un) -g $(id -gn) -m 755 /data/plx-releases

# 1. Cut the first release: origin/main on the served generation's schema (the two files
#    proactive-truth holds back, exactly what deploy_compat.sh did), node_modules from today's deploy.
git -C /data/OntologyProject fetch -q origin
git -C /data/OntologyProject worktree add -q --detach /data/scratch/wt/release-tool origin/main
REL=/data/scratch/wt/release-tool/deploy/release/plx-release
ID=$($REL build origin/main \
      --overlay 30e62f7251^1:ontology/schema/domains/purchasing.json \
      --overlay 30e62f7251^1:ontology/schema/process/po_delivery_window.json \
      --node-modules-from /data/scratch/wt/deploy/app/node_modules | tail -1)
$REL switch "$ID"                    # only the link moves; nothing restarts yet
REL=/data/plx-releases/current/deploy/release/plx-release

# 2. Keep the units as they are now, then render and install the release's units.
sudo tar -czf /root/plx-units-before-release.tgz /etc/systemd/system/plx-*
python3 /data/plx-releases/current/deploy/render.py --out /tmp/plx-units
sudo diff -ru /etc/systemd/system /tmp/plx-units | grep -E '^[-+](WorkingDirectory|ExecStart|ReadWritePaths)'
sudo cp -r /tmp/plx-units/. /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Restart the long-running units on the release. Add --with-ingest only when no build runs
#    (builds are off today: "builds": "off" in the ingest config).
$REL restart --with-ingest
#    The Postgres follower loop (a nohup process, not a unit) moves the same way:
kill "$(cat /data/scratch/pg-cutover/follow_now.pid)"
nohup /data/plx-releases/current/deploy/ops/pg_follow.sh >> /data/scratch/pg-cutover/follow.log 2>&1 &

# 4. User units (publish watcher, nightly suite) from the release.
python3 /data/plx-releases/current/deploy/render.py --user --out ~/.config/systemd/user
systemctl --user daemon-reload

# 5. Check: every process runs from the release, and every freshness target holds.
$REL status
/data/plx-releases/current/deploy/ops/ops_check.sh
```

The timers do not need a restart. A delta cycle that is already running finishes on the code
it started with, and the next cycle runs from `current`.

**Rollback.**
- To the previous release: `$REL rollback && $REL restart`.
- To the units as they were before the release, with code from `/data/scratch/wt/deploy`
  and `wt/search`: `sudo tar -xzf /root/plx-units-before-release.tgz -C /`, then
  `sudo systemctl daemon-reload`, then
  `plx-release restart --with-ingest` (the API through its warm switch: a direct `systemctl restart plx-api` is refused, plx-api-cycle.target).

After a day on the release, the old trees (`/data/scratch/wt/deploy`, `/data/scratch/wt/search`)
and `/data/scratch/cfg/*.sh` can be retired.

## Day to day

```sh
REL=/data/plx-releases/current/deploy/release/plx-release
/data/plx-releases/current/deploy/ops/deploy_compat.sh       # origin/main on the served schema: build, switch, restart, re-derive
# or by hand: ID=$($REL build origin/main | tail -1) && $REL switch $ID && $REL restart
$REL status; $REL list; $REL prune --keep 5
```

Landings use `deploy/ops/land.sh`, and the product-repo push uses `deploy/ops/rishi_push.sh`.
Both work as before. They read the scrub tool from `$PLX_OPS_PRIVATE`.

## Stall alerts

`/api/health` keeps `"status": "ok"` while the process serves. It also carries `ops`: every
freshness target with its value, target and state. The checks are:
- delta last-cycle age (target 15 min) and the running cycle's runtime (30 min);
- each capture source's age (6 h, and any source with `stale_since`);
- Postgres follow (more than 6 layers behind, or stalled for 30 min);
- the proactive engine's last pass (3 h);
- the truth check's contradicted share (25%);
- the AI watcher's last run (2 h);
- API page p95 over the last 15 min (5 s).

A full-read reader (the owner) sees any breach in the app's sidebar status line
("Ops: N behind"), with the details on click. To override the bounds, point
`PLX_OPS_TARGETS` at a JSON file in the API's environment. The API reads Postgres with
`PLX_OPS_PG_DSN` or `PLX_PG_DSN`.


```sh
/data/plx-releases/current/deploy/ops/ops_check.sh          # or --json
```

An outgoing alert, such as an email, needs the owner's OK first.

## Nightly full suite

`deploy/ops/suite_nightly.sh` runs every test module on origin/main in a detached worktree
(`/data/scratch/wt/suite-nightly`). It uses `tools/main_ci.py --slots --min-free-gb 10
--timings`, so the slowest modules start first, one process per module runs on each core,
each module is wrapped in testslot, and a module starts only while 10 GB of memory is free.
Results go to `/data/scratch/runs/suite/latest.{txt,json}`. Modules listed in
`tools/KNOWN-RED.md` are reported but do not fail the run.

```sh
python3 /data/plx-releases/current/deploy/render.py --user --out ~/.config/systemd/user
systemctl --user daemon-reload
systemctl --user enable --now plx-suite.timer            # 01:30 ET nightly
loginctl enable-linger "$(id -un)"                        # user timers run without a login session
systemctl --user start plx-suite.service                 # once now, optional
tail -1 /data/scratch/runs/suite/latest.txt
```

You can also run it by hand: `deploy/ops/suite_nightly.sh`. For only what a branch touches:
`python3 tools/main_ci.py --affected origin/main --slots`.

## Backups

The nightly backup and restore test are described in `deploy/backup/README.md`. They cover:
- Postgres DDL;
- reviews, the console journal, case-runtime cases, the gap inbox and delta state;
- the AI watcher store and switch;
- the app data dir with feedback and known readers;
- the config (secrets only encrypted, or listed).

Install them after step 1 above, which created the release:

```sh
python3 /data/plx-releases/current/deploy/render.py --out /tmp/plx-units
sudo install -m 644 /tmp/plx-units/plx-backup.{service,timer} /tmp/plx-units/plx-restore-test.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plx-backup.timer plx-restore-test.timer     # 03:15 and 04:30 ET
sudo systemctl start plx-backup.service && sudo systemctl start plx-restore-test.service
journalctl -u plx-restore-test -n 20 --no-pager                          # "problems": []
```
