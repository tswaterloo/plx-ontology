#!/bin/bash
# Host settings for every deploy/ops script, sourced (never executed). The values live in
# ${PLX_DEPLOY_ENV:-$HOME/.config/plx/deploy.env} on the host (KEY=value lines, see
# deploy/host.env.example), never in Git: no username or home path is committed.
# Every setting has a default that matches the single-host layout.
if [ -r "${PLX_DEPLOY_ENV:-$HOME/.config/plx/deploy.env}" ]; then
  set -a; . "${PLX_DEPLOY_ENV:-$HOME/.config/plx/deploy.env}"; set +a
fi
: "${PLX_USER:=$(id -un)}"
: "${PLX_GROUP:=$(id -gn)}"
: "${PLX_HOME:=$HOME}"
: "${PLX_CONFIG:=$PLX_HOME/.config/plx}"            # env files, host configs (secrets: never in Git)
: "${PLX_REPO:=/data/OntologyProject}"              # the git checkout releases are cut from (read only)
: "${PLX_RELEASES:=/data/plx-releases}"             # one directory per release, plus current/previous
: "${PLX_CURRENT:=$PLX_RELEASES/current}"           # what every unit runs
: "${PLX_STATE:=/data/plx-state}"
: "${PLX_VENV:=/data/venv}"
: "${PLX_NODE:=/data/tools/node}"
: "${PLX_SCRATCH:=/data/scratch}"                   # run logs, worktrees for landings
: "${PLX_RUNS:=$PLX_SCRATCH/runs}"
: "${PLX_OPS_PRIVATE:=$PLX_SCRATCH/cfg}"            # tools whose rule lists name people (scrub rules): host-only
: "${PLX_OPS:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ops}"   # these scripts
: "${PLX_API_URL:=http://127.0.0.1:8780}"
: "${PLX_TZ:=America/New_York}"
export PLX_USER PLX_GROUP PLX_HOME PLX_CONFIG PLX_REPO PLX_RELEASES PLX_CURRENT PLX_STATE \
  PLX_VENV PLX_NODE PLX_SCRATCH PLX_RUNS PLX_OPS PLX_OPS_PRIVATE PLX_API_URL PLX_TZ
plx_now() { TZ=$PLX_TZ date +%-I:%M\ %p; }
