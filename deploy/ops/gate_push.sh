#!/bin/bash
# usage: gate_push.sh <wt> [extra modules...]
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -u
export PATH=$PLX_VENV/bin:$PATH
cd "$1"; shift
mods=$(grep -o 'unittest .*> "\$out"' $PLX_OPS/land_batch.sh | sed 's/unittest //; s/ > "\$out"//')
out=$PLX_SCRATCH/runs/gp-$$.txt
env -i PATH=$PATH HOME=$HOME nice -n 5 python3 -m unittest $mods "$@" > $out 2>&1
grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)" $out
grep -qE "^OK" $out || { echo NOTOK; exit 1; }
git fetch -q origin main; git merge-base --is-ancestor origin/main HEAD || { echo "main moved"; exit 1; }
git push -q origin HEAD:main && echo "LANDED $(git rev-parse --short HEAD)"
