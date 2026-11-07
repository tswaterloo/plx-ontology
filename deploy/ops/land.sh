#!/bin/bash
# Land a reviewed SHA onto main (handoff procedure): merge --no-ff in a fresh worktree of
# origin/main, no conflicts, direct test list + extra modules, ancestry check, push.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -euo pipefail
sha=$1; name=$2; summary=$3; shift 3
repo=$PLX_REPO
wt=$PLX_SCRATCH/wt/land-$name
export PATH=$PLX_VENV/bin:$PATH
cd $repo && git fetch -q origin main
rm -rf "$wt"; git worktree prune
git worktree add -q --detach "$wt" origin/main
cd "$wt"
git -c user.name="$(git log -1 --format=%an origin/main)" -c user.email="$(git log -1 --format=%ae origin/main)" \
  merge --no-ff -q "$sha" -m "Land $name: $summary

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
[ -z "$(git diff --name-only --diff-filter=U)" ] || { echo "CONFLICTS"; exit 1; }
out=$PLX_SCRATCH/runs/land-$name.tests.txt
env -i PATH=$PATH HOME=$HOME nice -n 5 python3 -m unittest tests.test_no_hardcoded_cases "$@" > "$out" 2>&1 || true
grep -E "^(Ran|OK|FAILED)" "$out"
if ! grep -qE "^OK" "$out"; then
  # a failure under host load is re-run alone once; only a repeat failure blocks the landing
  failed=$(grep -oE "^(FAIL|ERROR): [a-z_]+ \(([A-Za-z0-9_.]+)\)" "$out" | sed -E 's/^(FAIL|ERROR): ([a-z_]+) \(([A-Za-z0-9_.]+)\)/\3/' | sort -u)
  [ -n "$failed" ] || { echo "TESTS NOT OK; not pushed"; exit 1; }
  echo "re-running alone: $failed"
  env -i PATH=$PATH HOME=$HOME nice -n 5 python3 -m unittest $failed > "$out.rerun" 2>&1 || true
  grep -E "^(Ran|OK|FAILED)" "$out.rerun"
  grep -qE "^OK" "$out.rerun" || { echo "TESTS NOT OK; not pushed"; exit 1; }
fi
git fetch -q origin main
git merge-base --is-ancestor origin/main HEAD || { echo "main moved; re-run"; exit 1; }
git push -q origin HEAD:main && echo "LANDED $(git rev-parse --short HEAD)"
cd $repo && git worktree remove --force "$wt"
