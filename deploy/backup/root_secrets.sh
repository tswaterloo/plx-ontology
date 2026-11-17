#!/usr/bin/env bash
# Root pre-step of plx-backup.service (ExecStartPre=+): the restore-needed /etc/plx files
# that stay 0600 root (the mint check refuses a looser mode) go into one encrypted archive
# the backup user carries (it holds the passphrase too, so this is not a secrecy boundary
# against that user, only against a read of /etc/plx). Exactly the files named below (the four
# root-only ones, backup-root.conf included, which the backup also scans), nothing globbed.
# Settings come only from the ROOT-owned /etc/plx/backup-root.conf (0600, root): the backup
# user's environment is never read. Keys: ROOT_SECRETS_DIR (default /run/plx-backup, the
# unit's RuntimeDirectory), ROOT_SECRETS_OWNER (backup user), ROOT_SECRETS_PASSPHRASE_FILE.
# The tar goes straight into gpg (no plaintext on disk); gpg writes into a fresh 0700
# root-made directory under /run (not user-writable), then the archive is chowned and renamed
# into place with mv -T (a rename replaces a planted symlink, never follows it).
set -euo pipefail
umask 077
conf=/etc/plx/backup-root.conf
[ -f "$conf" ] && [ ! -L "$conf" ] && [ "$(stat -c '%u %a' "$conf")" = "0 600" ] \
  || { echo "root_secrets: $conf missing or not root 0600, skipped" >&2; exit 0; }
get() { sed -n "s/^$1=//p" "$conf" | tail -1; }
out="$(get ROOT_SECRETS_DIR)"; out="${out:-/run/plx-backup}"
owner="$(get ROOT_SECRETS_OWNER)"
pass="$(get ROOT_SECRETS_PASSPHRASE_FILE)"
files=(/etc/plx/live-session.env /etc/plx/mint-readers.env /etc/plx/api-model.env /etc/plx/backup-root.conf)
[ -d "$out" ] && [ ! -L "$out" ] || { echo "root_secrets: $out is not a plain directory, skipped" >&2; exit 0; }
[ -n "$owner" ] && id -u "$owner" >/dev/null 2>&1 || { echo "root_secrets: no owner, skipped" >&2; exit 0; }
[ -n "$pass" ] && [ -r "$pass" ] || { echo "root_secrets: no passphrase file, skipped" >&2; exit 0; }
rm -f "$out/root-secrets.tar.gpg" "$out/root-secrets.list"
have=()
for f in "${files[@]}"; do [ -f "$f" ] && [ ! -L "$f" ] && have+=("$f"); done
[ "${#have[@]}" -gt 0 ] || exit 0
stage="$(mktemp -d /run/plx-root-secrets.XXXXXX)"; trap 'rm -rf "$stage"' EXIT
tar -cf - --numeric-owner -p -C / "${have[@]/#\//}" \
  | gpg --batch --yes --quiet --symmetric --cipher-algo AES256 --pinentry-mode loopback \
        --passphrase-file "$pass" -o "$stage/root-secrets.tar.gpg"
printf '%s\n' "${have[@]}" > "$stage/root-secrets.list"
chown --no-dereference "$owner" "$stage/root-secrets.tar.gpg" "$stage/root-secrets.list"
mv -T "$stage/root-secrets.tar.gpg" "$out/root-secrets.tar.gpg"
mv -T "$stage/root-secrets.list" "$out/root-secrets.list"
