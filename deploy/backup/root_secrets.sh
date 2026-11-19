#!/usr/bin/env bash
# Root pre-step of plx-backup.service (ExecStartPre=+): every file under /etc/plx goes into one
# encrypted archive the backup user carries (it holds the passphrase too, so this is not a
# secrecy boundary against that user, only against a read of /etc/plx). By rule, not by name
# (11-18: a new root-only file, ai-account-old.env, was not on the named list and the backup
# exited 7): every regular file under /etc/plx, sub-directories included (bak/), symlinks never
# followed, one file system. /etc/plx is root's own (only root can add a file to it), so the
# rule picks up nothing a user planted. The list (root-secrets.list) names each file, then each
# sub-directory with a trailing slash, so the backup can tell a directory it cannot read was
# carried here.
# Settings come only from the ROOT-owned /etc/plx/backup-root.conf (0600, root): the backup
# user's environment is never read. Keys: ROOT_SECRETS_DIR (default /run/plx-backup, the
# unit's RuntimeDirectory), ROOT_SECRETS_OWNER (backup user), ROOT_SECRETS_PASSPHRASE_FILE.
# The tar goes straight into gpg (no plaintext on disk); gpg writes into a fresh 0700
# root-made directory under /run (not user-writable), then the archive is chowned and renamed
# into place with mv -T (a rename replaces a planted symlink, never follows it).
# A skip or a failure never stops the backup itself (exit 0, the reason on stderr naming the
# file): the backup then lists every file it could not read as restore_files_missing and
# exits 7, and the alarm names them.
# Arguments (the unit passes none; only the root-owned unit file could): the directory
# (/etc/plx) and where the fresh staging directory is made (/run). The conf must belong to the
# user running this (root under the unit) and be 0600.
set -euo pipefail
umask 077
src="${1:-/etc/plx}"
run_dir="${2:-/run}"
conf=$src/backup-root.conf
[ -f "$conf" ] && [ ! -L "$conf" ] && [ "$(stat -c '%u %a' "$conf")" = "$(id -u) 600" ] \
  || { echo "root_secrets: $conf missing or not 0600 of $(id -un), skipped" >&2; exit 0; }
get() { sed -n "s/^$1=//p" "$conf" | tail -1; }
out="$(get ROOT_SECRETS_DIR)"; out="${out:-/run/plx-backup}"
owner="$(get ROOT_SECRETS_OWNER)"
pass="$(get ROOT_SECRETS_PASSPHRASE_FILE)"
[ -d "$out" ] && [ ! -L "$out" ] || { echo "root_secrets: $out is not a plain directory, skipped" >&2; exit 0; }
[ -n "$owner" ] && id -u "$owner" >/dev/null 2>&1 || { echo "root_secrets: no owner, skipped" >&2; exit 0; }
[ -n "$pass" ] && [ -r "$pass" ] || { echo "root_secrets: no passphrase file, skipped" >&2; exit 0; }
rm -f "$out/root-secrets.tar.gpg" "$out/root-secrets.list"
have=()
while IFS= read -r -d '' f; do
  if [ -r "$f" ]; then have+=("$f"); else echo "root_secrets: cannot read $f, left out" >&2; fi
done < <(find "$src" -xdev -type f -print0 | sort -z)
dirs=()
while IFS= read -r -d '' d; do dirs+=("$d/"); done < <(find "$src" -xdev -mindepth 1 -type d -print0 | sort -z)
[ "${#have[@]}" -gt 0 ] || exit 0
stage="$(mktemp -d "$run_dir/plx-root-secrets.XXXXXX")"; trap 'rm -rf "$stage"' EXIT
if ! printf '%s\0' "${have[@]#/}" \
    | tar -cf - --numeric-owner -p -C / --no-recursion --null -T - \
    | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --pinentry-mode loopback \
          --passphrase-file "$pass" -o "$stage/root-secrets.tar.gpg"; then
  echo "root_secrets: archiving ${have[*]} failed, nothing staged" >&2
  exit 0
fi
printf '%s\n' "${have[@]}" "${dirs[@]}" > "$stage/root-secrets.list"
chown --no-dereference "$owner" "$stage/root-secrets.tar.gpg" "$stage/root-secrets.list"
mv -T "$stage/root-secrets.tar.gpg" "$out/root-secrets.tar.gpg"
mv -T "$stage/root-secrets.list" "$out/root-secrets.list"
