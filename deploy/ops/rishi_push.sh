#!/bin/bash
# Push the product code folders of OntologyProject origin/main to plx-ontology main.
# Usage: rishi_push.sh "<commit message: what improved for users>" [--dry] [--no-verify]
#   1. git archive FOLDERS twice (orig, scrub); drop __pycache__.
#   2. rishi_scrub.py on the scrub copy: any unallowed hit or verification failure stops.
#   3. rishi_verify.sh orig scrub (unless --no-verify): no test may be worse scrubbed.
#   4. Copy the scrubbed folders in; scrub the repo's own docs in place (only a verification
#      failure stops; their remaining hits are listed for review); drop internal folders.
#   5. Secret scan over added lines, then commit and push (no force).
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set -euo pipefail
msg=${1:?commit message}; shift
dry=""; verify=1
for a in "$@"; do case $a in --dry) dry=1;; --no-verify) verify="";; esac; done
FOLDERS="app deploy evals examples integrations ontology operating operator_api operator_ui scripts tests twin verify"
DOCS="docs/reference context use-cases README.md"
DROP="docs/decisions"
exec 9>$PLX_SCRATCH/runs/rishi-push.lock; flock 9   # one push at a time: the stage folder is shared
src=$PLX_REPO; dst=/data/plx-ontology; stage=$PLX_SCRATCH/rishi-stage
cfg=$PLX_OPS; export PATH=$PLX_VENV/bin:$PATH
cd $src && git fetch -q origin main
ref=${SRC_REF:-origin/main}; src_sha=$(git rev-parse --short $ref)
rm -rf $stage && mkdir -p $stage/orig $stage/scrub
# host-specific deploy files and the internal live-check tooling stay internal
EXCLUDE=(':!deploy/ops' ':!deploy/user' ':!deploy/systemd/host' ':!deploy/systemd/plx-memlog.service' ':!deploy/DEMO-HOST.md' ':!verify/live_verify.py' ':!verify/data_profile.py' ':!deploy/systemd/plx-ingest.service' ':!deploy/systemd/plx-delta.service' ':!deploy/systemd/plx-delta.timer' ':!deploy/systemd/plx-delta-compact.service' ':!deploy/systemd/plx-delta-compact.timer' ':!deploy/systemd/plx-truth-check.service' ':!deploy/systemd/plx-truth-check.path' ':!deploy/systemd/plx-truth-check.timer' ':!deploy/systemd/plx-search-follow.service' ':!deploy/systemd/plx-search-follow.path' ':!deploy/systemd/plx-search-follow.timer' ':!deploy/systemd/plx-ai-watcher.service' ':!deploy/systemd/plx-ai-watcher.timer' ':!deploy/systemd/plx-ontology-pg-follow.service' ':!deploy/systemd/plx-ontology-pg-follow.timer' ':!deploy/systemd/plx-search-generation.service' ':!deploy/systemd/plx-search-generation.path' ':!deploy/systemd/plx-search-generation.timer' ':!deploy/systemd/plx-rag-backfill.service' ':!deploy/systemd/plx-proactive.service' ':!deploy/systemd/plx-proactive.timer' ':!deploy/backup/plx-backup.service' ':!deploy/backup/plx-backup.timer' ':!deploy/backup/plx-restore-test.service' ':!deploy/backup/plx-restore-test.timer')
git archive $ref -- $FOLDERS "${EXCLUDE[@]}" | tar -x -C $stage/orig
git archive $ref -- $FOLDERS "${EXCLUDE[@]}" | tar -x -C $stage/scrub
for t in orig scrub; do
  find $stage/$t -name __pycache__ -type d -prune -exec rm -rf {} +
  find $stage/$t -name "*.pyc" -delete
done
python3 $PLX_OPS_PRIVATE/rishi_scrub.py $stage/scrub --report $stage/scrub-report.json > $stage/scrub-out.txt 2>&1 \
  || { echo "CODE SCRUB NOT CLEAN: see $stage/scrub-out.txt"; grep UNALLOWED $stage/scrub-out.txt | head; exit 1; }
tail -1 $stage/scrub-out.txt
if [ -n "$verify" ]; then
  $cfg/rishi_verify.sh $stage/orig $stage/scrub > $stage/verify.txt 2>&1 \
    || { echo "SCRUBBED TESTS WORSE: see $stage/verify.txt"; tail -8 $stage/verify.txt; exit 1; }
  grep -E "^== |WORSE" $stage/verify.txt
fi
cd $dst && git pull -q --ff-only origin main
for f in $FOLDERS; do rm -rf "$dst/$f"; cp -a "$stage/scrub/$f" "$dst/$f"; done
for d in $DROP; do [ -e "$dst/$d" ] && git rm -rq "$d"; done
mkdir -p $stage/docs && for d in $DOCS; do mkdir -p "$stage/docs/$(dirname $d)"; cp -a "$dst/$d" "$stage/docs/$d"; done
set +e; python3 $PLX_OPS_PRIVATE/rishi_scrub.py $stage/docs --report $stage/docs-report.json > $stage/docs-out.txt 2>&1; rc=$?; set -e
[ $rc -eq 2 ] && { echo "DOC SCRUB VERIFICATION FAILED: $stage/docs-out.txt"; git reset -q --hard; exit 1; }
grep UNALLOWED $stage/docs-out.txt | cut -c1-140 || true
for d in $DOCS; do rm -rf "$dst/$d"; cp -a "$stage/docs/$d" "$dst/$d"; done
git add -A $FOLDERS $DOCS
# secret scan over what would be committed (added lines only)
if git diff --cached -U0 | grep -E '^\+' | grep -nE 'sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{40,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[bp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY' | head -5; then
  echo "SECRET PATTERN FOUND: not committed"; git reset -q --hard; exit 1
fi
git diff --cached --stat | tail -1
[ -n "$dry" ] && { echo "dry run: staged from $src_sha, not committed (git -C $dst reset --hard to undo)"; exit 0; }
git -c user.name="$(git log -1 --format=%an)" -c user.email="$(git log -1 --format=%ae)" commit -qm "$msg

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
timeout 120 git push -q origin HEAD:main && echo "PUSHED $(git rev-parse --short HEAD) (code from OntologyProject $src_sha)"
