#!/bin/bash
# Runs ON a Spark: the app's typecheck and vitest for <sha> (spark_test.sh <sha> --app).
# usage: spark_app_remote.sh <sha> [typecheck|test|both]   (default both)
# node_modules is installed once per package-lock.json (npm ci) under ~/plx-tests/app-modules/<lock hash>
# and linked into the checkout, so a second run of the same lockfile installs nothing.
set -uo pipefail
sha=$1 what=${2:-both}
T=$HOME/plx-tests
wt=$T/wt/app-$sha
mkdir -p "$T/wt" "$T/app-modules"
if [ ! -d "$wt" ]; then git -C "$T/repo.git" worktree prune; git -C "$T/repo.git" worktree add -q --detach "$wt" "$sha" || { echo "checkout failed" >&2; exit 97; }; fi
ls -1dt "$T"/wt/app-*/ 2>/dev/null | tail -n +5 | while read -r d; do [ -n "$(find "$d" -maxdepth 0 -mmin +180)" ] || continue; git -C "$T/repo.git" worktree remove --force "$d" 2>/dev/null || rm -rf "$d"; done
for v in $(compgen -e | grep -E '(KEY|SECRET|TOKEN|PASSWORD|PASSWD|DSN|CREDENTIAL)'); do unset "$v"; done
cd "$wt/app" || exit 97
lock=$(sha256sum package-lock.json | cut -c1-16)
nm=$T/app-modules/$lock
if [ ! -d "$nm/node_modules" ]; then
  mkdir -p "$nm.tmp.$$" && cp package.json package-lock.json "$nm.tmp.$$/" && (cd "$nm.tmp.$$" && npm ci --no-audit --no-fund --silent) \
    || { rm -rf "$nm.tmp.$$"; echo "npm ci failed" >&2; exit 98; }
  mv -T "$nm.tmp.$$" "$nm" 2>/dev/null || rm -rf "$nm.tmp.$$"
fi
ln -sfn "$nm/node_modules" node_modules
rc=0; t0=$(date +%s)
if [ "$what" = typecheck ] || [ "$what" = both ]; then npm run -s typecheck && echo "typecheck: ok" || { echo "typecheck: FAIL"; rc=1; }; fi
if [ "$what" = test ] || [ "$what" = both ]; then npm test --silent 2>&1 | tail -25; [ "${PIPESTATUS[0]}" = 0 ] && echo "vitest: ok" || { echo "vitest: FAIL"; rc=1; }; fi
echo "app ${sha:0:10} $what exit $rc ($(( $(date +%s) - t0 )) s)"
exit $rc
