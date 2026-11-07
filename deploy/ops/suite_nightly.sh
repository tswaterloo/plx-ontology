#!/bin/bash
# The whole test suite on origin/main, every test module in its own process across all
# cores (and a module starts only
# while PLX_SUITE_MIN_FREE_GB of memory is available). Tests the tree of a detached
# worktree (never the dev checkout), so a landing mid-run cannot mix versions.
# Writes $PLX_SUITE_OUT/suite-<stamp>.{txt,json} and latest.{txt,json}; the last line of the
# .txt is the count ("N modules: P pass, K known red, F FAIL"). Exit: main_ci's (1 on an
# unlisted red module). Run by the user timer deploy/user/plx-suite.timer.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -euo pipefail
out=${PLX_SUITE_OUT:-$PLX_RUNS/suite}
wt=${PLX_SUITE_TREE:-$PLX_SCRATCH/wt/suite-nightly}
ref=${PLX_SUITE_REF:-origin/main}
mkdir -p "$out"
git -C "$PLX_REPO" fetch -q origin
if [ ! -e "$wt/.git" ]; then git -C "$PLX_REPO" worktree add -q --detach "$wt" "$ref"; fi
git -C "$wt" checkout -qf --detach "$ref"
git -C "$wt" clean -qfdx -e app/node_modules
stamp=$(date -u +%Y%m%dT%H%M%SZ)
set +e
PATH="$PLX_VENV/bin:$PATH" nice -n 5 python3 "$wt/devloop/main_ci.py" --repo "$wt" \
  --workers "${PLX_SUITE_WORKERS:-$(nproc)}" --slots --min-free-gb "${PLX_SUITE_MIN_FREE_GB:-10}" \
  --timings "$out/timings.json" --report "$out/suite-$stamp.json" > "$out/suite-$stamp.txt" 2>&1
code=$?
set -e
ln -sfn "suite-$stamp.txt" "$out/latest.txt"; ln -sfn "suite-$stamp.json" "$out/latest.json"
echo "$(plx_now) suite on $(git -C "$wt" rev-parse --short HEAD): $(grep -E '^[0-9]+ modules:' "$out/suite-$stamp.txt" | tail -1) (exit $code)"
ls -1t "$out"/suite-*.txt 2>/dev/null | tail -n +31 | while read -r old; do rm -f "$old" "${old%.txt}.json"; done
exit $code
