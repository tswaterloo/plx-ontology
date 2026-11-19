#!/bin/bash
# Run an eval, bench, replay or any read-only job ON A SPARK instead of the box (the box keeps
# Postgres, capture and the app; the job's Python, its tool hosts and its Claude sessions use the
# Spark's 20 cores).
#   spark_run.sh [--host s7d3d|sb4ec] [--sha <commit>] [--ship <path>]... [--out <dir>]...
#                [--env NAME]... [--state copy|none] [--pg box|standby] [--name <label>] -- <command> [args...]
# Postgres: every run logs in as plx_eval, a FULL reader (read-only): fine for owner-level evals
#   and benches. Evals as a staff or restricted reader stay on the box until a scoped Spark
#   role exists (the relay maps every Spark login to plx_eval).
# --pg standby (or SPARK_PG=standby): read the Spark's hot standby of the box when its guard
#   passes (standby_guard.py: in recovery, slot kept, replay within 10 min), else the box, said
#   on stderr. Default: the box.
# The sandbox is read-only except the --out folders, a /tmp of the run's own, the Claude
#   homes (Claude writes its session files there) and the Spark's state copy (never copied back).
# Inside the job the box's paths hold (deploy/spark/spark_run_remote.py builds the sandbox):
#   /data/OntologyProject and the caller's checkout path = <commit> (default: HEAD of the
#   checkout you run from); /data/venv = the Spark's Python; /data/plx-state = the Spark's copy
#   of the box's stores (spark_state_sync.py; --state none: empty); postgresql:///plx = the box's
#   Postgres as plx_eval (read-only, enforced on the box by the relay's peer map, whatever user
#   the job asks for); `claude` and the Workbench/autopilot Claude homes with the current access
#   token (spark_cred_sync.sh); `sudo -n -u X cmd` runs cmd.
# --ship <path>: a file or folder (scripts, host configs, question sets) copied to the Spark at the
#   same path before the run. --out <dir>: shipped too, and copied BACK to the box after the run
#   (only these come back). Paths under /data/plx-state, /etc, /var/lib or a home's .ssh/.claude
#   are refused (use --state).
# --env NAME: pass that variable's value (it travels on ssh's stdin, never a command line or a
#   file on the Spark). Without --env only TZ/LANG and PLX_* variables that do not look like
#   secrets pass; a secret (KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|DSN in the name) only by --env.
# Output streams back; exit code = the job's (96 = no Spark reachable, 97 = setup failed).
# Example (an Ask bench as the owner, 4 workers):
#   deploy/spark/spark_run.sh --sha HEAD --ship /data/scratch/my-job/bench.py --out /data/scratch/my-job/out \
#     --env PLX_WORKBENCH_SESSION_SECRET --env PLX_EVAL_READER -- python3 /data/scratch/my-job/bench.py ...
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
ssh_cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
host=${SPARK_HOST:-s7d3d} sha="" state=copy pg=${SPARK_PG:-box} name="" ships=() outs=() envs=()
while [ $# -gt 0 ]; do case "$1" in
  --host) host=$2; shift 2 ;; --sha) sha=$2; shift 2 ;; --ship) ships+=("$2"); shift 2 ;;
  --out) outs+=("$2"); shift 2 ;; --env) envs+=("$2"); shift 2 ;; --state) state=$2; shift 2 ;;
  --pg) pg=$2; shift 2 ;;
  --name) name=$2; shift 2 ;; --) shift; break ;; -h|--help) sed -n 2,32p "$0"; exit 2 ;;
  *) echo "spark_run: unknown option $1 (the command goes after --)" >&2; exit 2 ;; esac; done
[ $# -gt 0 ] || { echo "usage: spark_run.sh [options] -- <command> [args...]" >&2; exit 2; }
case "$host" in s7d3d|sb4ec) ;; *) echo "spark_run: --host is s7d3d or sb4ec" >&2; exit 2 ;; esac
case "$state" in copy|none) ;; *) echo "spark_run: --state is copy or none" >&2; exit 2 ;; esac
case "$pg" in box|standby) ;; *) echo "spark_run: --pg is box or standby" >&2; exit 2 ;; esac
top=$(git rev-parse --show-toplevel 2>/dev/null || echo /data/OntologyProject)
sha=$(git -C "$top" rev-parse --verify -q "${sha:-HEAD}^{commit}") || { echo "spark_run: unknown commit ${sha:-HEAD}" >&2; exit 2; }
[[ "$PWD" == "$top" || "$PWD" == "$top"/* ]] && cwd=$PWD || cwd=$top
refuse() { echo "spark_run: refused $1: $2" >&2; exit 2; }
norm() { realpath -m -- "$1"; }
paths=() ship_paths=() out_paths=()
for p in "${ships[@]+"${ships[@]}"}" "${outs[@]+"${outs[@]}"}"; do
  r=$(norm "$p"); [[ "$r" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || refuse "$p" "path characters"
  case "$r" in /|/data|/data/plx-state|/data/plx-state/*|/etc|/etc/*|/var/lib/*|/root*|*/.ssh|*/.ssh/*|*/.claude|*/.claude/*|*/.config/plx/*.env|/data/OntologyProject|/data/OntologyProject/*)
    refuse "$p" "not shippable (state comes from --state; code from --sha)" ;; esac
  paths+=("$r")
done
for p in "${ships[@]+"${ships[@]}"}"; do ship_paths+=("$(norm "$p")"); done
for p in "${outs[@]+"${outs[@]}"}"; do out_paths+=("$(norm "$p")"); done
for o in "${outs[@]+"${outs[@]}"}"; do mkdir -p "$(norm "$o")"; done
for p in "${ships[@]+"${ships[@]}"}"; do [ -e "$p" ] || refuse "$p" "does not exist"; done
R="ssh -F $ssh_cfg -o BatchMode=yes -o ConnectTimeout=15 $host"
$R true 2>/dev/null || { echo "spark_run: $host unreachable (exit 96)" >&2; exit 96; }
# The Spark's Claude login (spark_cred_sync.sh's copy) must be present and not about to run
# out, else every Claude call of the job answers "Not logged in": stop here, loudly, instead of
# producing those results. SPARK_SKIP_LOGIN_CHECK=1 for a job that makes no Claude call.
if [ "${SPARK_SKIP_LOGIN_CHECK:-0}" != 1 ]; then
  login_msg=$($R python3 - 2>&1 <<'PY'
import json, os, sys, time
bad = []
for d in ("plx-workbench", "autopilot"):
    p = os.path.expanduser("~/plx-ai/home/%s/.claude/.credentials.json" % d)
    try:
        left = json.load(open(p))["claudeAiOauth"]["expiresAt"] / 1000 - time.time()
    except Exception as e:
        bad.append("%s: unreadable (%s)" % (d, type(e).__name__))
        continue
    if left < 120:
        bad.append("%s: login expired or expiring (%d s left)" % (d, left))
print("; ".join(bad))
sys.exit(1 if bad else 0)
PY
  ) || { echo "spark_run: the Claude login on $host is not valid ($login_msg); run the login sync (systemctl start plx-spark-cred-sync) or set SPARK_SKIP_LOGIN_CHECK=1 for a job without Claude calls (exit 95)" >&2; exit 95; }
fi
# the commit's objects only (as spark_test.sh): no data, no secrets
GIT_SSH_COMMAND="ssh -F $ssh_cfg" git -C "$top" push -q "$host:plx-tests/repo.git" "$sha:refs/plx/$sha" \
  || { echo "spark_run: push to $host failed" >&2; exit 97; }
rh=$(cat "$here/spark_run_remote.py" "$here/standby_guard.py" | sha256sum | cut -c1-12)
$R "test -s ~/plx-run/r-$rh/spark_run_remote.py" 2>/dev/null || {
  $R "mkdir -p ~/plx-run/r-$rh" && scp -q -F "$ssh_cfg" "$here/standby_guard.py" "$host:plx-run/r-$rh/.guard.$$" \
    && scp -q -F "$ssh_cfg" "$here/spark_run_remote.py" "$host:plx-run/r-$rh/.remote.$$" \
    && $R "mv -f ~/plx-run/r-$rh/.guard.$$ ~/plx-run/r-$rh/standby_guard.py && mv -f ~/plx-run/r-$rh/.remote.$$ ~/plx-run/r-$rh/spark_run_remote.py"; } || { echo "spark_run: copy to $host failed" >&2; exit 97; }
for r in "${paths[@]+"${paths[@]}"}"; do
  [ -e "$r" ] || continue
  $R "mkdir -p -m 700 ~/plx-run/files$(dirname "$r")" && \
  rsync -a --delete -e "ssh -F $ssh_cfg -o BatchMode=yes" "$r" "$host:plx-run/files$(dirname "$r")/" \
    || { echo "spark_run: shipping $r failed" >&2; exit 97; }
done
# the run description, one JSON line on stdin (variable values never on a command line)
spec=$(SPEC_SHA=$sha SPEC_TREE=$top SPEC_CWD=$cwd SPEC_STATE=$state SPEC_PG=$pg SPEC_NAME=${name:-$(basename "$top")} \
  python3 "$here/spark_run_spec.py" "${#ship_paths[@]}" "${ship_paths[@]+"${ship_paths[@]}"}" \
  "${#out_paths[@]}" "${out_paths[@]+"${out_paths[@]}"}" "${#envs[@]}" "${envs[@]+"${envs[@]}"}" "$@") || exit 2
# memory guard (as spark_test.sh): the run's scope holds MemoryMax = MemAvailable - 16 GB
started=$(date +%s)
printf '%s\n' "$spec" | $R "mem=\$(awk '/MemAvailable/ {print int(\$2/1048576)}' /proc/meminfo); cap=\$((mem-16)); [ \$cap -ge 4 ] || cap=4;
  exec systemd-run --user --scope -q -p MemoryMax=\${cap}G -- python3 ~/plx-run/r-$rh/spark_run_remote.py"
rc=$?
for o in "${outs[@]+"${outs[@]}"}"; do
  r=$(norm "$o")
  rsync -a -e "ssh -F $ssh_cfg -o BatchMode=yes" "$host:plx-run/files$r/" "$r/" || { echo "spark_run: copying $r back failed" >&2; [ $rc != 0 ] || rc=97; }
done
echo "spark_run ${sha:0:10} on $host exit $rc ($(( $(date +%s) - started )) s)" >&2
exit $rc
