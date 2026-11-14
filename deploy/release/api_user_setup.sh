#!/bin/bash
# SEC-S-07: what the API's own user (plx-workbench) needs on the host, and the check that it
# still holds. Root runs it; deploy/release/README-api-user.md says when.
#
#   sudo deploy/release/api_user_setup.sh            dry run: prints every change, makes none
#   sudo deploy/release/api_user_setup.sh --apply    makes them (idempotent)
#   deploy/release/api_user_setup.sh --check         any user: lists what drifted (exit 1)
#
# The shared-write set W is what the API writes in the state directory (measured 11-13 with
# deploy/pg/role_walk.py --files-out, plus the routes that write: sign-in, feedback, work log,
# autopilot decisions, view-as audit, send journal): group plx-state (the jobs' user and the
# API's user), group read/write, setgid directories so new files keep the group. The state
# root gets the sticky and setgid bits: the API may create its own files there (an SQLite
# journal, the send journal), in the shared group, and remove only those. Everything else in
# the state directory (capture stores, projection, search, evaluation sets) stays the jobs'
# user's alone: the API reads it through its unit's read capability.
#
# The read-store set R is the exception: live SQLite stores in WAL mode (capture stores, the
# projection's side stores, the AI watcher's). A WAL reader, even one opened mode=ro, takes
# its read locks in the store's -shm file and creates -wal/-shm when the last writer removed
# them, so a reader that cannot write them fails on its first read (SQLITE_READONLY_DIRECTORY,
# found 11-13 by the plx-workbench trial; immutable=1 would read a live store inconsistently).
# R gets: the directory group plx-state, g+rwx, setgid (new -wal/-shm keep the group) and
# sticky (the API cannot remove or rename the jobs' files); its SQLite files (top level only)
# group read/write. SQLite creates -wal/-shm with the database file's mode, so they stay
# group read/write. Blobs and other files stay the jobs' user's, read through the capability.
# Residual: at the file-permission level the API's user could write those SQLite files and
# create new files in R directories (e.g. a -wal next to a store no writer has open); it
# cannot remove or rename the jobs' files (sticky). It opens the capture stores mode=ro.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
API_USER=${PLX_API_USER:-plx-workbench}
GROUP=${PLX_STATE_GROUP:-plx-state}
S=$PLX_STATE
W_DIRS=(case-runtime feedback host work-log autopilot-write view-as actions app-data case-workspace-live)
W_FILES=(console-journal.sqlite console-ticket-audit.jsonl session-replay.sqlite reviews.sqlite)
R_DIRS=(bc-capture mail-capture sharepoint-capture sharepoint-list-capture teams-capture
        meetings-capture mc-capture projection ai-watcher autopilot)
# the top-level SQLite files of an R directory (databases and their -wal/-shm/-journal)
r_files() { find "$1" -maxdepth 1 -type f \( -name '*.sqlite' -o -name '*.sqlite-*' -o -name '*.db' \
              -o -name '*.db-*' \) ! -name '*.lock' "${@:2}" 2>/dev/null; }
mode=dry
case ${1:-} in --apply) mode=apply ;; --check) mode=check ;; "") ;; *) echo "usage: $0 [--apply|--check]" >&2; exit 2 ;; esac

run() { if [ "$mode" = apply ]; then "$@"; else echo "would: $*"; fi; }

if [ "$mode" = check ]; then
  bad=0
  getent passwd "$API_USER" >/dev/null || { echo "no user $API_USER"; bad=1; }
  getent group "$GROUP" >/dev/null || { echo "no group $GROUP"; bad=1; }
  id -nG "$PLX_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP" || { echo "$PLX_USER not in $GROUP"; bad=1; }
  id -nG "$API_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$GROUP" || { echo "$API_USER not in $GROUP"; bad=1; }
  [ "$(stat -c %G "$S")" = "$GROUP" ] && [ -k "$S" ] && [ -g "$S" ] \
    && [ "$(( 0$(stat -c %a "$S") & 070 ))" = 56 ] \
    || { echo "$S: want group $GROUP, g=rwx, setgid, sticky"; bad=1; }
  # a file or directory of W the API could not write (a job made it with a private umask)
  for d in "${W_DIRS[@]}"; do
    [ -e "$S/$d" ] || continue
    n=$(find "$S/$d" \( ! -group "$GROUP" -o ! -perm -g+rw -o \( -type d ! -perm -g+xs \) \) -print 2>/dev/null | wc -l)
    [ "$n" = 0 ] || { echo "$S/$d: $n entries not group $GROUP read/write"; bad=1; }
  done
  for f in "${W_FILES[@]}"; do
    [ -e "$S/$f" ] || continue
    [ "$(stat -c %G "$S/$f")" = "$GROUP" ] && [ -n "$(find "$S/$f" -perm -g+rw)" ] \
      || { echo "$S/$f: not group $GROUP read/write"; bad=1; }
  done
  for d in "${R_DIRS[@]}"; do
    [ -d "$S/$d" ] || continue
    { [ "$(stat -c %G "$S/$d")" = "$GROUP" ] && [ -g "$S/$d" ] && [ -k "$S/$d" ] \
      && [ -n "$(find "$S/$d" -maxdepth 0 -perm -g+rwx)" ]; } \
      || { echo "$S/$d: want group $GROUP, g=rwx, setgid, sticky"; bad=1; }
    n=$(r_files "$S/$d" \( ! -group "$GROUP" -o ! -perm -g+rw \) -print | wc -l)
    [ "$n" = 0 ] || { echo "$S/$d: $n SQLite files not group $GROUP read/write"; bad=1; }
  done
  [ "$bad" = 0 ] && echo "api user setup holds"
  exit $bad
fi

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
# the state directory's owner is the jobs' user, never root (deploy.env names it)
[ "$PLX_USER" != root ] || { echo "PLX_USER is root: run through sudo from the host user, or set PLX_DEPLOY_ENV" >&2; exit 2; }
getent group "$GROUP" >/dev/null || run groupadd --system "$GROUP"
getent passwd "$API_USER" >/dev/null || run useradd --system --user-group --home-dir /var/lib/plx-workbench \
  --no-create-home --shell /usr/sbin/nologin "$API_USER"
run usermod -aG "$GROUP" "$API_USER"
run usermod -aG "$GROUP" "$PLX_USER"
run chgrp "$GROUP" "$S"
run chmod g+rwxs,+t "$S"     # setgid: what the API creates in the root keeps the shared group
for d in "${W_DIRS[@]}"; do
  [ -e "$S/$d" ] || run install -d -o "$PLX_USER" -g "$GROUP" -m 2770 "$S/$d"
  run chgrp -R "$GROUP" "$S/$d"
  run chmod -R g+rwX "$S/$d"
  run find "$S/$d" -type d -exec chmod g+s {} +
done
for f in "${W_FILES[@]}"; do
  for g in "$S/$f" "$S/$f-wal" "$S/$f-shm" "$S/$f-journal"; do
    [ -e "$g" ] || continue
    run chgrp "$GROUP" "$g"
    run chmod g+rw "$g"
  done
done
for d in "${R_DIRS[@]}"; do
  [ -d "$S/$d" ] || continue
  run chgrp "$GROUP" "$S/$d"
  run chmod g+rwxs,+t "$S/$d"
  while IFS= read -r f; do
    run chgrp "$GROUP" "$f"
    run chmod g+rw "$f"
  done < <(r_files "$S/$d")
done
echo "$([ "$mode" = apply ] && echo applied || echo "dry run: nothing changed"); check: $0 --check"
