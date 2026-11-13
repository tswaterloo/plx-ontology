# Backups of the Workbench server

Rishi (29 Oct 2026): backups in three places (a local copy plus two other regions or
providers), nightly Postgres dumps and state backups, and a restore test.

## What is backed up (nightly, 03:15 ET; restore test nightly, 04:30 ET)

- **Postgres `plx`** (`pg_dump -Fc` on one exported snapshot): the DDL of every schema and
  the extensions; the data of `plx_meta` (and `public`). The data of `search` and
  `ontology` (~100 GB, rebuilt by the projection build and the search follower) is left out
  by default; `PLX_BACKUP_PG_SCHEMA_ONLY=` (empty) dumps everything. The manifest holds the
  exact row count of every table whose data is dumped (null: schema only).
- **Small state** under `/data/plx-state` (online SQLite backups on read-only connections,
  never a copy of a live WAL file; then one tar.gz, ~7 MB): `console-journal.sqlite` (the
  console's actions journal), `projection/ontology.sqlite.gap-inbox.sqlite` (gaps, acks,
  visits, history), `projection/CURRENT`, `projection/delta/CURRENT.json` and
  `cycles.jsonl` (delta state, not the delta layers), `host/host.sqlite`,
  `carried-cloud/host.sqlite`, `reviews.sqlite`, `case-runtime/{cases,originals,
  kind-confirmations}.sqlite` and `.cursors`, `session-replay.sqlite`,
  `console-ticket-audit.jsonl`, `search/follow.json`, `truth-check/{status.json,report.md}`
  and small JSON files; `actions.sqlite` and `actions/journal.jsonl` when they exist; the AI
  watcher store and its switch (`ai-watcher/watcher.sqlite`, `ai-watcher/switch.json`); and
  the app's data dir `${XDG_STATE_HOME:-~/.local/state}/plx-workbench-app` (feedback,
  known readers, test records), archived under `extra/`. A file over 512 MB is refused.
- **Config**: `~/.config/plx`, `/etc/plx`, `/etc/systemd/system/plx-*` as a tar with modes,
  owners and times kept. Only an allowlist is stored in plain text: unit files (`.service`,
  `.timer`, `.path`) and the Caddyfile whose text assigns no key/secret/token/password/DSN/
  authorization value, plus systemd drop-ins (`<unit>.d/*.conf`) and the reader-policy files
  (`host.json`, `employee_policy.json`, `portal_access.json`, `*_audience.json`) under the same
  screen. A backup that would leave out one of these (a secret-shaped line, an unreadable
  file) and has no passphrase to encrypt it exits 7 and lists it as `restore_files_missing`.
  Every other file (JSON, `.conf`, env files, anything unfamiliar) is
  a secret-class file, whatever its content looks like (the name screen is a second check,
  not the boundary): with
  `PLX_BACKUP_SECRETS_PASSPHRASE_FILE` set it goes to `secrets-<stamp>.tar.gpg` (gpg
  symmetric AES256; keep the passphrase off this machine too), else it is only listed in the
  manifest (path, mode, size).
- NOT backed up (large, rebuilt from the source systems): the projection store and delta
  layers, `case-runtime/source-mirror.sqlite` (62 GB), the capture stores.

## Layout, rotation, disk budget

`/data/backups/daily/` holds the 7 newest sets, `/data/backups/weekly/` the 4 newest weekly
sets (hard links). A set is `plx-<stamp>.dump`, `state-<stamp>.tar.gz`,
`config-<stamp>.tar.gz`, optionally `secrets-<stamp>.tar.gpg`, and `manifest-<stamp>.json`
(written last; a set without it is incomplete). Then the oldest sets are removed while all
exceed `PLX_BACKUP_MAX_GB` (20). No run starts when `/data` would keep less than
`PLX_BACKUP_MIN_FREE_GB` (200) free (exit 5).

## Run by hand

    deploy/backup/backup.sh          # 0 ok, 3 a state store missing, 4 off-site failed,
                                     # 5 disk guard, 6 pg dump failed (state+config kept)
    deploy/backup/restore_test.sh    # newest set: digests, dump into a throwaway DB (dropped),
                                     # PRAGMA integrity_check of each restored SQLite store

## Install (system units, run as $PLX_USER)

The units run from the current release (`deploy/release/plx-release`, docs/release.md) and
are rendered from templates with the host user's settings (`~/.config/plx/deploy.env`):

    python3 $PLX_RELEASES/current/deploy/render.py --out /tmp/plx-units
    sudo install -m 644 /tmp/plx-units/plx-backup.{service,timer} \
         /tmp/plx-units/plx-restore-test.{service,timer} /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now plx-backup.timer plx-restore-test.timer
    sudo systemctl start plx-backup.service && sudo systemctl start plx-restore-test.service
    systemctl list-timers 'plx-backup*' 'plx-restore*'

Settings go in `~/.config/plx/backup.env` (mode 600), e.g. `PLX_BACKUP_OFFSITE_HOOK=` or
`PLX_BACKUP_SECRETS_PASSPHRASE_FILE=`. The units never read `plx.env`.

## Restore

Stop writers first: `sudo systemctl stop plx-api plx-ingest plx-delta.timer
plx-delta-compact.timer plx-search-follow.timer plx-search-follow.path plx-truth-check.path`.
Pick a set: `S=/data/backups/daily; T=<stamp>`.

1. Verify: `deploy/backup/restore_test.sh` (newest set) or compare
   `sha256sum $S/*-$T.*` with `manifest-$T.json`.
2. State: `mkdir /tmp/r && tar -xzf $S/state-$T.tar.gz -C /tmp/r`; for each store to
   restore, move the live file and its `-wal`/`-shm` aside, then
   `install -m 600 /tmp/r/<rel> /data/plx-state/<rel>` (`extra/data/...` goes back to
   `/data/...`). Each restored `.sqlite` is a complete, checkpointed database.
3. Config: `sudo tar -xpzf $S/config-$T.tar.gz -C /` (modes and owners kept); secrets:
   `gpg -d --pinentry-mode loopback --passphrase-file <pass> $S/secrets-$T.tar.gpg |
   sudo tar -xpf - -C /`, or re-create the files the manifest lists (`config.secret_files`)
   from the vault with the listed modes.
4. Postgres schema: `createdb plx_new && pg_restore --no-owner -d plx_new $S/plx-$T.dump`,
   point `PLX_PG_DSN` at it, `python3 -m operating.pg status`, and let the ontology load and
   `plx-search-follow` re-fill the data from the served projection; a full-data dump
   (`PLX_BACKUP_PG_SCHEMA_ONLY=`) restores directly.
5. Start the services again; `systemctl status 'plx-*'`.

## Off-site copies: OPEN, needs the owner

Two off-site destinations (another region or provider each, e.g.
The hook is ready: copy
`offsite_hook.example.sh`, fill in both destinations, and set `PLX_BACKUP_OFFSITE_HOOK`.
Until then every run reports `"offsite": "not_configured"`. Encrypt before upload if the
destination is not encrypted at rest under PLX control (the state holds business text).
