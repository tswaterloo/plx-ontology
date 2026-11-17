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

## Off-box copy to a Spark (owner, 15 Nov 2026; no S3)

`offbox_copy.py` (units `plx-offbox-copy`, daily 6:45 AM ET, and `plx-offbox-restore-test`,
daily 12:30 PM ET) copies the newest healthy nightly set and the capture backup to the DGX
Spark sb4ec (`PLX_OFFBOX_HOSTS`, sb4ec only), over the tailnet ssh. Each file
is encrypted on this box (gpg AES256, key `~/.config/plx/offbox.key`, 0600, made once with
`offbox_copy.py init-key`, never printed; its one escrow copy is described below), sent with `rsync --checksum` into
`~/plx-offbox` (0700, ours, files 0600) and kept only after the Spark's sha256 matches.
Objects are content-addressed, so an unchanged piece is sent once; the newest 7 records are
kept. Green only when the sources are whole and every object of the record matched on the
Spark in that run; otherwise the unit fails (exit 3 incomplete, 1 failed) and the plx-
OnFailure alarm fires. Status: `~/.local/state/plx-offbox/{status,restore-status}.json`.

Data host and key never on one machine (review 459af42e5c): s7d3d holds the key's escrow
copy, so it is never a data host. A host listed in both `PLX_OFFBOX_HOSTS` and
`PLX_OFFBOX_ESCROW_HOSTS` (s7d3d) is refused before any connection, and a run fails before
any upload when a configured data host holds `~/.config/plx/offbox.key`. The host of the
first copy is kept; when it is unreachable the run FAILS (exit 1, the alarm) and never moves
the copy to another Spark.

Speed: one ssh stream over the userspace tailnet carries ~0.7 MB/s (a per-flow limit, not
the CPU or the cipher), so batches go out on 8 separate ssh connections (~5.4 MB/s measured
15 Nov; `PLX_OFFBOX_STREAMS`, at most 12: 16 made the jump host's sshd drop connections). The
Postgres set goes first, and a newer healthy set that lands during a long run goes to the
head of the queue and replaces the older set in that run's record. `PLX_OFFBOX_BWLIMIT_KBPS`
(unit: 4096 KiB/s over all streams, split per stream) caps the upload, so a reseed after a
Spark reset does not starve the embedding and test traffic; it takes about 4-5 h for ~59 GB.
Partial uploads an interrupted run leaves in `incoming/` are removed after 12 h
(`PLX_OFFBOX_INCOMING_MAX_HOURS`). The restore test tries the newest record first, then each
older kept record until one passes, and reports the newest, the one that passed and every
one tried; a corrupt record fails it even when an older one passes.

Known limits: a capture list file (`lists/*.gz`) is copied only when the frozen capture
manifest names its digest (the piece's `listing`) and the bytes read match it; a list with no
digest (an adopted piece) is left out and counted in `capture.lists_unverifiable` (the
capture backup re-reads the archive without it). The secrets tarball and the nightly
manifest carry no digest of their own; they are encrypted as read. The Spark sees ciphertext
sizes and record timestamps, never names or contents.

Key escrow (owner, 15 Nov 2026): the data is on sb4ec and the second copy of the key is on
the OTHER Spark, `s7d3d:~/.config/plx/offbox.key` (directory 0700, file 0600, our user), so
losing one machine (this box or either Spark) leaves the data readable, while neither Spark
holds both the data and the key. It was copied over ssh stdin and checked by comparing sha256
on both ends; the key is never printed. Check it (prints only MATCH or DIFFERENT):

    F=/data/plx-state/ts-petra/ssh_config
    [ "$(sha256sum < ~/.config/plx/offbox.key)" = "$(ssh -F $F s7d3d 'sha256sum < ~/.config/plx/offbox.key')" ] && echo MATCH || echo DIFFERENT

Open risk (decision for the owner and Vince): an admin with root on both Sparks, or one
account that reaches both, could take the key from s7d3d and the ciphertext from sb4ec and
read every backup, including excluded people's capture. The code only keeps the two on
different machines; it cannot stop a person who controls both. Mitigation: move the key's
second copy off the Sparks (the owner's password vault or another machine neither Spark
admin controls), then remove it from s7d3d. There is no S3, so an off-Spark escrow needs a
place Vince provides: on the Vince list.

The backup secrets passphrase (`~/.config/plx/backup-secrets.pass`, the only passphrase of
the nightly `secrets-<stamp>.tar.gpg`) is escrowed the same way: a second copy at
`s7d3d:~/.config/plx/backup-secrets.pass` (0600, ours; copied over ssh stdin on 15 Nov 2026
and checked by sha256, never printed), never on sb4ec. Check it the same way:

    [ "$(sha256sum < ~/.config/plx/backup-secrets.pass)" = "$(ssh -F $F s7d3d 'sha256sum < ~/.config/plx/backup-secrets.pass')" ] && echo MATCH || echo DIFFERENT

The daily restore test proves recovery without this box: besides the sampled objects it
checks on s7d3d (`PLX_OFFBOX_ESCROW_HOSTS`) that the escrowed key matches this box's key and
both escrowed files are ours, 0600, in a 0700 directory, and it opens the record's secrets
archive with the ESCROWED passphrase (read over ssh into a 0600 file in its private work
directory, deleted after). A missing, unsafe, different or non-opening escrow copy, or an
unreachable s7d3d, fails the test (alarm). A new passphrase must be escrowed the same day.

The owner may keep a third copy of both in his vault. A new key (`init-key` again) makes the old
Spark copy unreadable with the new key: escrow the new key the same way the same day.

Recovery after losing this box: on a new box (or on s7d3d itself) take the key from s7d3d
(`install -m 600` it to `~/.config/plx/offbox.key`, or point `PLX_OFFBOX_KEY` at it) and the
secrets passphrase (`install -m 600` it to `~/.config/plx/backup-secrets.pass`), set
`PLX_OFFBOX_HOSTS=sb4ec` and run `offbox_copy.py restore-test` to prove the newest record
decrypts. Then `rsync` the record (`copies/<stamp>.json.gpg`) and every object it names from
`sb4ec:~/plx-offbox/` back, `gpg -d --batch --pinentry-mode loopback --passphrase-file <key>`
each, check each plaintext sha256 against the record, and put each file at its `rel` path
(`daily/...` under /data/backups, `capture/...` under /var/backups/plx-capture); then the
restores above (Postgres first). The secrets archive opens with the escrowed passphrase:
`gpg -d --batch --pinentry-mode loopback --passphrase-file ~/.config/plx/backup-secrets.pass
daily/secrets-<stamp>.tar.gpg | tar -x -C <restore dir>`. Without this box's ssh_config, reach sb4ec through s7d3d
(`ssh -J <user>@<s7d3d> <user>@192.168.100.11`; the tailnet refuses ssh straight to sb4ec).

## Root-only /etc/plx files (live-session.env, mint-readers.env, api-model.env, backup-root.conf)

These stay 0600 root (plx-mint-link refuses a looser mode). `plx-backup.service` runs
`root_secrets.sh` as `ExecStartPre=+` (root). Its settings (archive directory, backup user,
passphrase file) come only from the root-owned `/etc/plx/backup-root.conf` (0600 root, keys
`ROOT_SECRETS_DIR`, `ROOT_SECRETS_OWNER`, `ROOT_SECRETS_PASSPHRASE_FILE`), never from the
backup user's environment. The script pipes a tar of exactly those four files (the conf itself too, since the backup scans /etc/plx and would otherwise list it as missing) into gpg
(AES256, the secrets passphrase), writes it in a fresh root-made directory under /run, chowns
it and renames it to `/run/plx-backup/root-secrets.tar.gpg` (+ `.list`); the backup user
carries it. It is encrypted at rest in the set, but the backup user also holds the passphrase, so this is no secrecy boundary against that user, only against reading the files in /etc/plx. `plx_backup.py` nests that archive inside
`secrets-<stamp>.tar.gpg` and records the staged paths as `config.root_staged` in the
manifest. A restore-needed file the run cannot read and the root step did not stage (no
conf, no passphrase, stale or foreign-owned archive, a hand run outside systemd) is always
`restore_files_missing` and the run exits 7. The restore test decrypts the nested archive and
fails unless every staged file is present and non-empty. No file mode changes.
