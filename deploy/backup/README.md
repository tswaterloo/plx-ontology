# Backups of the Workbench server

Rishi (29 Oct 2026): backups in three places (a local copy plus two other regions or
providers), nightly Postgres dumps and state backups, and a restore test.

## What is backed up

- The Postgres database `plx` (`pg_dump -Fc`, on one exported snapshot; the manifest holds
  the exact row count of every table read in that snapshot).
- The small state stores under `/data/plx-state` (online SQLite backups, then one tar.gz):
  `reviews.sqlite` (40 KB), `case-runtime/originals.sqlite` (28 KB),
  `case-runtime/cases.sqlite` (432 KB) and `.cursors` (12 KB),
  `case-runtime/kind-confirmations.sqlite` (28 KB), `host/host.sqlite` (52 KB),
  `carried-cloud/host.sqlite` (272 KB), `session-replay.sqlite` (128 KB) and three small
  JSON files. Sizes as of 29 Oct 2026; a file over 512 MB is refused.
- NOT backed up here (large, rebuilt from the source systems): `projection/` (41 GB),
  `case-runtime/source-mirror.sqlite` (35 GB), `mail-capture/` (2.5 GB),
  `sharepoint-capture/` (1.9 GB), `case-originals/` (1.3 GB), `teams-capture/` (498 MB),
  `bc-capture/` (158 MB), `mc-capture/` (86 MB). Decide separately if a rebuild is too slow.

## Layout and rotation

`/data/backups/daily/` holds the 7 newest sets, `/data/backups/weekly/` the 4 newest weekly
sets (hard links). A set is `plx-<stamp>.dump`, `state-<stamp>.tar.gz` and
`manifest-<stamp>.json` (written last; a set without it is incomplete).

## Run by hand

    deploy/backup/backup.sh          # exit 0 ok, 3 a listed state store missing, 4 off-site failed
    deploy/backup/restore_test.sh    # restores the newest set into plx_restore_test_<pid>, drops it

## Enable the timers (not enabled by the builder)

The units are user units and point at `/data/OntologyProject/deploy/backup/`.

    mkdir -p ~/.config/systemd/user ~/.config/plx
    cp deploy/backup/plx-backup.{service,timer} deploy/backup/plx-restore-test.{service,timer} \
       ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now plx-backup.timer plx-restore-test.timer
    loginctl enable-linger "$USER"     # so user timers run without a login session
    systemctl --user list-timers 'plx-*'

Settings go in `~/.config/plx/backup.env` (mode 600), for example `PLX_BACKUP_DIR=` or
`PLX_BACKUP_OFFSITE_HOOK=`.

## Off-site copies: OPEN, needs the owner

Two off-site destinations (another region or provider each, e.g.
The hook is ready: copy
`offsite_hook.example.sh`, fill in both destinations, and set `PLX_BACKUP_OFFSITE_HOOK`.
Until then every run reports `"offsite": "not_configured"`. Encrypt before upload if the
destination is not encrypted at rest under PLX control (the dump holds business text).
