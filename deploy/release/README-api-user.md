# The API as its own user, from a sealed release (SEC-S-07, S-12, S-13, REL-R-12)

Today the API runs as the host owner from a scratch worktree (`/data/plx-releases/current` is a
link to `/data/scratch/wt/deploy`), with `ProtectHome=no` and a superuser Postgres login. Any
agent can change the code it runs, and a compromise of the API reads `~/.ssh`, `~/.claude` and
every key. After these steps:

- the code is a **sealed release**: built unprivileged, then owned by root with nothing
  writable by anyone else (`plx-release verify`). A switch refuses a release that is not sealed;
- the API runs as **plx-workbench**. It can read the stores through `CAP_DAC_READ_SEARCH`,
  but only what its mount namespace shows. The owner's home is an empty tmpfs except the host
  config and the facts cache, both read-only. `/data/scratch`, backups, the dev checkout,
  Postgres data files and host secrets are not visible. It writes only its shared-write set in
  `/data/plx-state` (`api_user_setup.sh`) and its own `/var/lib/plx-workbench`;
- the API logs in to Postgres as **plx_api** (no superuser, no `COPY ... PROGRAM`);
- the **live session secret** is root's alone (0600). Links come from `sudo plx-mint-link`,
  which mints the owner reader only. A person's link needs that person at a terminal.

Residual: the host owner account keeps passwordless sudo, so an agent shell can still become
root. These steps protect the API and the code from each other and from the owner's files.
They do not protect anything from root.

Run the steps in order. Each step can be rolled back on its own. Times are ET. Nothing here
changes Postgres data or any business system.

## 0.
- No ingest build is running. Builds are off.

## 1. Postgres role (S-13)
```sh
psql -X -v ON_ERROR_STOP=1 -d plx -f /data/OntologyProject/deploy/pg/plx_api_role.sql   # idempotent
# walk Home, record pages, search and Ask under the role on a dev API (spare port, dev secret):
python3 /data/OntologyProject/deploy/pg/role_walk.py --port 8791      # exit 0 = nothing denied
```
A denial prints the GRANT to add to `plx_api_role.sql` and its test. Fix it and walk again.
Then map the API's OS users to the role. Put the map above the generic `local` line:
```sh
PGDATA=$(psql -XAtc 'show data_directory' -d plx)
sudo cp $PGDATA/pg_hba.conf $PGDATA/pg_hba.conf.bak-s13; sudo cp $PGDATA/pg_ident.conf $PGDATA/pg_ident.conf.bak-s13
#   pg_ident.conf:  plxapi  plx-workbench  plx_api
#                   plxapi  <owner>        plx_api        (until step 4, the API is still the owner)
#   pg_hba.conf:    local  plx  plx_api  peer map=plxapi
sudo systemctl reload postgresql
psql -X -d 'postgresql:///plx?user=plx_api' -Atc 'select current_user'          # plx_api
```
The API logs in as the role through `PGUSER`. `PLX_PG_DSN` is set in three env files, and
an env file overrides `Environment=`, so the DSN is left as it is:
```sh
python3 /data/OntologyProject/deploy/render.py --api-user --out /tmp/plx-units
sudo install -m 644 /tmp/plx-units/plx-api.service.d/zy-pg-role.conf /etc/systemd/system/plx-api.service.d/
sudo systemctl daemon-reload && sudo systemctl restart plx-api
sudo grep 'permission denied' /var/log/postgresql/postgresql-17-main.log      # after Home, a record, Ask
```
**Rollback:** `sudo rm /etc/systemd/system/plx-api.service.d/zy-pg-role.conf`, then daemon-reload
and restart plx-api. The two `.bak-s13` files undo the map.

## 2. Sealed releases (S-12, REL-R-12)
```sh
sudo chown root:root /data/plx-releases && sudo chmod 755 /data/plx-releases
sudo chown -h root:root /data/plx-releases/current
sudo install -d -o $(id -un) -g $(id -gn) -m 700 /data/plx-releases/.staging
```
Then follow `docs/release.md`, "Switch-over", steps 1-5, unchanged. `plx-release build` now
builds in `.staging` and seals the release with `sudo -n`. The first release copies
`node_modules` from the unsealed seed (about 0.9 GB). Later releases hard-link it from the
sealed current release. After step 1, check the seal:
`/data/plx-releases/current/deploy/release/plx-release verify`. If plx-app logs `EACCES`
under `.next` after step 3, roll back below and report it. The app writes nothing at runtime
today: there is no `next/image` and no ISR.
**Rollback:** `docs/release.md` "Rollback": `sudo tar -xzf /root/plx-units-before-release.tgz -C /`,
`sudo systemctl daemon-reload`, restart the units, then
`sudo ln -sfn /data/scratch/wt/deploy /data/plx-releases/current`.

### 2b. The interpreter and node
The API runs `/data/venv/bin/python3` and the app runs `/data/tools/node`. Both trees belong
to the host owner, so an agent could change code the sealed API imports. Make them root's.
Agents and jobs can still read and run them. A `pip install` then needs sudo.
`/data` itself belongs to the host owner, who could rename a root-owned tree away and put
another in its place. Make `/data` root's and sticky, with the owner's group allowed to write.
The owner can still create and remove their own entries. Root's entries (`plx-releases`,
`venv`, `tools`) cannot be renamed.
```sh
sudo chown -R root:root /data/venv /data/tools/node && sudo chmod -R go-w /data/venv /data/tools/node
sudo chown root:root /data/tools && sudo chmod 755 /data/tools
sudo chown root:$(id -gn) /data && sudo chmod 1771 /data
```
`plx-mint-link` refuses to mint until the release and the venv are both sealed and every
directory above them is root's (and sticky when others may write it).
**Rollback:** `sudo chown -R $(id -un):$(id -gn) /data/venv /data/tools && sudo chown $(id -un):$(id -gn) /data && sudo chmod 751 /data`.

## 3. The API's user and its state rights (S-07, part 1: changes nothing that runs)
```sh
R=/data/plx-releases/current
sudo $R/deploy/release/api_user_setup.sh             # dry run: read what it will change
sudo $R/deploy/release/api_user_setup.sh --apply     # user plx-workbench, group plx-state, W set
$R/deploy/release/api_user_setup.sh --check          # "api user setup holds"
# Ask's Claude Code: a system copy of the CLI and a long-lived token (the owner runs
# `claude setup-token` once; the value goes only into this root file)
sudo install -m 755 "$(readlink -f ~/.local/bin/claude)" /usr/local/bin/claude
sudo install -m 600 /dev/null /etc/plx/api-model.env
sudoedit /etc/plx/api-model.env                      # CLAUDE_CODE_OAUTH_TOKEN=<token>
```
The backup keeps the app data where it now lives. Add a drop-in to `plx-backup.service`:
`Environment=PLX_BACKUP_EXTRA_FILES=/data/plx-state/app-data` and
`Environment=PLX_APP_DATA_DIR=/data/plx-state/app-data`.
**Rollback:** nothing to undo. The group bits do not change what the owner's API can do.

## 4. Trial: the same unit as plx-workbench on 8792, development secret (S-07, part 2)
```sh
python3 $R/deploy/render.py --api-user --out /tmp/plx-units
U=/etc/systemd/system
sudo cp $U/plx-api.service $U/plx-api-trial.service
sudo mkdir -p $U/plx-api-trial.service.d
sudo cp $U/plx-api.service.d/*.conf /tmp/plx-units/plx-api.service.d/zz-api-user.conf $U/plx-api-trial.service.d/
sudo rm $U/plx-api-trial.service.d/live-secret.conf                  # the trial signs with the dev secret
sudo cp /tmp/plx-units/plx-api-trial.service.d/zzz-trial.conf $U/plx-api-trial.service.d/
sudo systemctl daemon-reload && sudo systemctl start plx-api-trial
python3 $R/deploy/pg/role_walk.py --api-url http://127.0.0.1:8792 --unit plx-api-trial
sudo systemctl stop plx-api-trial
```
The walk must exit 0: no database denial, no file denial (a path the API's user may not
write), and no route at 500. A file denial names the path. Add it to `W_DIRS`/`W_FILES` in
`api_user_setup.sh`, apply again, then walk again. Ask must end on `answer`. A missing token
fails here, not in production. The trial's failure also raises the ops unit alarm
(`plx-.service.d/onfailure.conf`). The trial and its model server need about 6 GB, so run it
while no build runs.

## 5. Switch the live API (S-07, part 3)
```sh
sudo systemctl stop plx-api
# the app data dir moves into the shared-write set (the owner's copy stays as the rollback)
cp -a ~/.local/state/plx-workbench-app/. /data/plx-state/app-data/
sudo chgrp -R plx-state /data/plx-state/app-data && sudo chmod -R g+rwX /data/plx-state/app-data
sudo cp /etc/plx/workbench-api.env /etc/plx/workbench-api.env.bak-s07
# PLX_API_ARGS: --app-data-dir /data/plx-state/app-data (the rest unchanged)
sudoedit /etc/plx/workbench-api.env
sudo install -m 644 /tmp/plx-units/plx-api.service.d/zz-api-user.conf $U/plx-api.service.d/
sudo chown root:root /etc/plx/live-session.env && sudo chmod 600 /etc/plx/live-session.env
sudo install -m 755 $R/deploy/ops/plx-mint-link /usr/local/sbin/plx-mint-link
sudo install -m 600 /dev/null /etc/plx/mint-readers.env
sudoedit /etc/plx/mint-readers.env       # PLX_EVAL_READER=<the owner reader's claims, from plx.env>
sudo systemctl daemon-reload && sudo systemctl start plx-api
ps -o user= -p "$(systemctl show -p MainPID --value plx-api)"          # plx-workbench
curl -s http://127.0.0.1:8780/api/health
$R/deploy/ops/access_link.sh            # now through sudo plx-mint-link (owner reader only)
```
Then open Home, a record page and Ask in the app.
`/data/scratch/cfg/{eval_login.sh,live-access-link.sh,dev_access_link.sh}` read the live secret
directly. Point them at `sudo -n /usr/local/sbin/plx-mint-link` (owner only), or the evals
stop at exit 3.
**Rollback:**
`sudo rm $U/plx-api.service.d/zz-api-user.conf && sudo cp /etc/plx/workbench-api.env.bak-s07 /etc/plx/workbench-api.env && sudo chown root:$(id -gn) /etc/plx/live-session.env && sudo chmod 640 /etc/plx/live-session.env && sudo systemctl daemon-reload && sudo systemctl restart plx-api`.
The state rights from step 3 can stay.

## Known costs and checks
- The API's user has its own search model server (`/tmp/plx-search-models-<uid>`, private
  `/tmp`): about 3.4 GB more resident on the host.
- The Postgres facts cache is bound read-only. The follower writes it ahead, and the API only
  reads it. On a cache miss the API recomputes and does not write. Watch the first record read
  after a layer lands.
- A job that creates a new file in the shared-write set with a private umask makes it
  unwritable for the API. `api_user_setup.sh --check` lists such files. Run it after deploys.
