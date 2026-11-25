#!/bin/bash
# Run an eval, bench, replay or any read-only job ON A SPARK instead of the box (the box keeps
# Postgres, capture and the app; the job's Python, its tool hosts and its Claude sessions use the
# Spark's 20 cores).
#   spark_run.sh [--host s7d3d|sb4ec] [--sha <commit>] [--ship <path>]... [--out <dir>]...
#                [--env NAME]... [--state copy|none] [--pg box|standby] [--name <label>] [--spark-out <dir>]...
#                [--cleanup-ship] -- <command> [args...]
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
# --spark-out <dir>: a folder that LIVES on the Spark (~/plx-run/files<dir>), bound writable in the sandbox and
#   never shipped from the box or copied back (a long-lived runner's own output: the box fetches it, as
#   operating/pg/plan_sections_pull.py does); created there when missing.
# --env NAME: pass that variable's value (it travels on ssh's stdin, never a command line or a
#   file on the Spark). Without --env only TZ/LANG and PLX_* variables that do not look like
#   secrets pass; a secret (KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|DSN in the name) only by --env.
# Sandbox: auto (default) = bwrap where it starts. On a Spark where it cannot (sb4ec: AppArmor blocks
#   user namespaces) the job runs with NO SANDBOX: deploy/spark/pathmap.c (LD_PRELOAD, built on the
#   Spark) maps the same binds, refuses writes under read-only ones, TMPDIR is the run's own tmp, and
#   Postgres is the Spark's hot standby as plx_eval (guard fallback: the box balancer, said on stderr).
#   --no-sandbox forces that mode; --sandbox fails instead of running without one.
# Output streams back; exit code = the job's (96 = no Spark reachable, 97 = setup failed).
# Example (an Ask bench as the owner, 4 workers):
#   deploy/spark/spark_run.sh --sha HEAD --ship /data/scratch/my-job/bench.py --out /data/scratch/my-job/out \
#     --env PLX_WORKBENCH_SESSION_SECRET --env PLX_EVAL_READER -- python3 /data/scratch/my-job/bench.py ...
set -uo pipefail
here=$(dirname "$(readlink -f "$0")")
ssh_cfg=${PLX_SPARK_SSH_CONFIG:-/data/plx-state/ts-petra/ssh_config}
host=${SPARK_HOST:-s7d3d} sha="" state=copy pg=${SPARK_PG:-box} name="" sandbox=auto cleanup_ship=0 ships=() outs=() envs=() sparkouts=()
while [ $# -gt 0 ]; do case "$1" in
  --host) host=$2; shift 2 ;; --sha) sha=$2; shift 2 ;; --ship) ships+=("$2"); shift 2 ;;
  --out) outs+=("$2"); shift 2 ;; --env) envs+=("$2"); shift 2 ;; --state) state=$2; shift 2 ;;
  --pg) pg=$2; shift 2 ;; --spark-out) sparkouts+=("$2"); shift 2 ;;
  --name) name=$2; shift 2 ;; --no-sandbox) sandbox=never; shift ;; --sandbox) sandbox=always; shift ;;
  --cleanup-ship) cleanup_ship=1; shift ;; --) shift; break ;; -h|--help) sed -n 2,40p "$0"; exit 2 ;;
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
for p in "${ships[@]+"${ships[@]}"}" "${outs[@]+"${outs[@]}"}" "${sparkouts[@]+"${sparkouts[@]}"}"; do
  r=$(norm "$p"); [[ "$r" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || refuse "$p" "path characters"
  case "$r" in /|/data|/data/plx-state|/data/plx-state/*|/etc|/etc/*|/var/lib/*|/root*|*/.ssh|*/.ssh/*|*/.claude|*/.claude/*|*/.config/plx/*.env|/data/OntologyProject|/data/OntologyProject/*)
    refuse "$p" "not shippable (state comes from --state; code from --sha)" ;; esac
done
for p in "${ships[@]+"${ships[@]}"}" "${outs[@]+"${outs[@]}"}"; do paths+=("$(norm "$p")"); done
for p in "${ships[@]+"${ships[@]}"}"; do ship_paths+=("$(norm "$p")"); done
for p in "${outs[@]+"${outs[@]}"}"; do out_paths+=("$(norm "$p")"); done
spark_out_paths=(); for p in "${sparkouts[@]+"${sparkouts[@]}"}"; do spark_out_paths+=("$(norm "$p")"); done
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
rh=$(cat "$here/spark_run_remote.py" "$here/standby_guard.py" "$here/pathmap.c" | sha256sum | cut -c1-12)
$R "test -s ~/plx-run/r-$rh/spark_run_remote.py" 2>/dev/null || {
  $R "mkdir -p ~/plx-run/r-$rh" && scp -q -F "$ssh_cfg" "$here/standby_guard.py" "$host:plx-run/r-$rh/.guard.$$" \
    && scp -q -F "$ssh_cfg" "$here/pathmap.c" "$host:plx-run/r-$rh/.pathmap.$$" \
    && scp -q -F "$ssh_cfg" "$here/spark_run_remote.py" "$host:plx-run/r-$rh/.remote.$$" \
    && $R "mv -f ~/plx-run/r-$rh/.pathmap.$$ ~/plx-run/r-$rh/pathmap.c && mv -f ~/plx-run/r-$rh/.guard.$$ ~/plx-run/r-$rh/standby_guard.py && mv -f ~/plx-run/r-$rh/.remote.$$ ~/plx-run/r-$rh/spark_run_remote.py"; } || { echo "spark_run: copy to $host failed" >&2; exit 97; }
shipped_clean=()
errlog=""
cleanup() {
  [ -n "$errlog" ] && rm -f "$errlog"
  if [ "$cleanup_ship" = 1 ] && [ "${#shipped_clean[@]}" -gt 0 ]; then
    local q="" r
    for r in "${shipped_clean[@]}"; do
      q="$q ~/plx-run/files$r"
    done
    $R "rm -rf --$q" 2>/dev/null || true
  fi
}
trap cleanup EXIT
for r in "${paths[@]+"${paths[@]}"}"; do
  [ -e "$r" ] || continue
  if [ "$cleanup_ship" = 1 ]; then
    for s in "${ship_paths[@]+"${ship_paths[@]}"}"; do
      if [ "$s" = "$r" ]; then shipped_clean+=("$r"); break; fi
    done
  fi
  $R "mkdir -p -m 700 ~/plx-run/files$(dirname "$r")" && \
  rsync -a --delete -e "ssh -F $ssh_cfg -o BatchMode=yes" "$r" "$host:plx-run/files$(dirname "$r")/" \
    || { echo "spark_run: shipping $r failed" >&2; exit 97; }
done
for r in "${spark_out_paths[@]+"${spark_out_paths[@]}"}"; do
  $R "mkdir -p -m 700 ~/plx-run/files$r" || { echo "spark_run: creating $r on $host failed" >&2; exit 97; }
done
# the run description, one JSON line on stdin (variable values never on a command line)
spec=$(SPEC_SHA=$sha SPEC_TREE=$top SPEC_CWD=$cwd SPEC_STATE=$state SPEC_PG=$pg SPEC_NAME=${name:-$(basename "$top")} SPEC_SANDBOX=$sandbox \
  python3 "$here/spark_run_spec.py" "${#ship_paths[@]}" "${ship_paths[@]+"${ship_paths[@]}"}" \
  "$(( ${#out_paths[@]} + ${#spark_out_paths[@]} ))" "${out_paths[@]+"${out_paths[@]}"}" "${spark_out_paths[@]+"${spark_out_paths[@]}"}" "${#envs[@]}" "${envs[@]+"${envs[@]}"}" "$@") || exit 2
# memory guard (as spark_test.sh): the run's scope holds MemoryMax = MemAvailable - 16 GB, and
# the run is the kernel's first choice under memory pressure (oom_score_adj 800: 11-22 an eval
# beside the s7d3d standby got the standby OOM-killed; a user scope cannot protect the standby
# itself, so the runs volunteer first)
started=$(date +%s)
errlog=$(mktemp)
printf '%s\n' "$spec" | $R "mem=\$(awk '/MemAvailable/ {print int(\$2/1048576)}' /proc/meminfo); cap=\$((mem-16)); [ \$cap -ge 4 ] || cap=4;
  exec systemd-run --user --scope -q -p MemoryMax=\${cap}G -- choom -n 800 -- python3 ~/plx-run/r-$rh/spark_run_remote.py" \
  2> >(tee "$errlog" >&2)
rc=$?
# a broken standby.json on the Spark (its guard says so on stderr) raises one ops alarm per change
sleep 0.3
python3 "$here/standby_settings_alarm.py" "$host" "$errlog" $([ "$pg" = standby ] && echo --asked) || true
rm -f "$errlog"
errlog=""
for o in "${outs[@]+"${outs[@]}"}"; do
  r=$(norm "$o")
  rsync -a -e "ssh -F $ssh_cfg -o BatchMode=yes" "$host:plx-run/files$r/" "$r/" || { echo "spark_run: copying $r back failed" >&2; [ $rc != 0 ] || rc=97; }
done
echo "spark_run ${sha:0:10} on $host exit $rc ($(( $(date +%s) - started )) s)" >&2
exit $rc
